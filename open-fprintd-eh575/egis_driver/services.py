import logging
import queue
import threading
import time
from collections import Counter

from egis_driver import egis_driver, fingerprint_matcher
from egis_driver.capture import CaptureCoordinator, CaptureOutcome
from egis_driver.persistence import Persistence
from egis_driver.runtime_config import RuntimePaths
from egis_driver.sensor_controller import SensorCanceled, SensorController
from egis_driver.streaming import (
    CapturePump,
    DirectMatcherWorker,
    FrameStatus,
    MatcherWorker,
    TouchMatcherWorker,
)
from egis_matcher.policy import ConfirmationPolicy, ConfirmationTracker

logger = logging.getLogger("SERVICE")

ENROLL_STAGES = 10
VERIFY_FRAME_COUNT = 3
VERIFY_CONFIRMATION_ATTEMPTS = 3
PRODUCTION_CONFIRMATION_POLICY = ConfirmationPolicy(
    frames_per_attempt=VERIFY_FRAME_COUNT,
    required_consecutive_accepts=VERIFY_CONFIRMATION_ATTEMPTS,
)
RESUME_RECOVERY_WAIT_SECONDS = 3.0
RESUME_RECOVERY_WARMUP_FRAMES = 4
RESUME_USB_SETTLE_SECONDS = 2.0
VERIFY_IDLE_LOG_INTERVAL_SECONDS = 10.0
VERIFY_PRESENCE_THRESHOLD = 24.0
VERIFY_PRESENCE_DELTA = 8.0
VERIFY_BASELINE_SAMPLE_COUNT = 12
SCAN_STOP_TIMEOUT_SECONDS = 2.0


class ScanOperation:
    def __init__(self, generation, args):
        self.generation = generation
        self.args = args
        self.mode = args[0]
        self.cancel_event = threading.Event()
        self.thread = None
        self.terminal = False
        self.sensor = None


class EgisService:
    """Core domain logic for the Egis EH575 fingerprint sensor.

    Handles enrollment, verification, sensor health, and scan loops.
    Communicates results via callbacks — has zero knowledge of D-Bus.
    """

    def __init__(self, driver=None, matcher=None, persistence=None,
                 on_enroll_status=None, on_verify_status=None,
                 on_verify_finger_selected=None, runtime_paths=None,
                 capture_coordinator=None, confirmation_policy=None,
                 matcher_worker=None):
        runtime_paths = runtime_paths or RuntimePaths.from_environment()
        self._persistence = persistence or Persistence(str(runtime_paths.data_root))
        self._sensor = (SensorController(backend=driver) if driver is not None
                        else SensorController(backend_factory=egis_driver.EgisDriver))
        try:
            self._matcher = matcher or fingerprint_matcher.FingerprintMatcher(
                persistence=self._persistence,
                frame_spec=self._sensor.frame_spec)
            if matcher_worker is not None:
                self._matcher_worker = matcher_worker
            elif matcher is not None:
                self._matcher_worker = DirectMatcherWorker(self._matcher)
            else:
                self._matcher_worker = MatcherWorker(
                    self._persistence.root_dir,
                    self._sensor.frame_spec,
                    self._matcher.matcher_config,
                )
        except BaseException:
            self._sensor.close()
            raise
        self._capture = capture_coordinator or CaptureCoordinator(self._sensor.session())
        self._confirmation_policy = (
            confirmation_policy or PRODUCTION_CONFIRMATION_POLICY)
        self._match_mode = runtime_paths.match_mode
        self._touch_matcher_worker = None
        if matcher is None and self._match_mode != "window":
            try:
                self._touch_matcher_worker = TouchMatcherWorker(
                    self._persistence.root_dir,
                    self._sensor.frame_spec,
                    self._matcher.matcher_config,
                )
                self._touch_matcher_worker.start()
            except Exception as error:
                logger.warning("Touch matcher shadow unavailable: %s", error)
                if self._touch_matcher_worker is not None:
                    self._touch_matcher_worker.close()
                self._touch_matcher_worker = None

        self._operation_lock = threading.RLock()
        self._operation_generation = 0
        self._active_operation = None
        self._scan_thread = None
        self._suspended_operation = None
        self._enroll_scans = []
        self._enroll_touch_count = 0
        self._resume_lock = threading.Lock()
        self._resume_ready = threading.Event()
        self._resume_ready.set()
        self._sensor_ready = threading.Event()
        self._sensor_ready.set()
        self._resume_recovery_thread = None
        self._resume_recovery_running = False
        self._resume_generation = 0
        self._resume_cancel = threading.Event()
        self._closed = False
        self._verify_session_id = 0
        self._capture_epoch = 0
        self._match_outcomes = Counter()
        self._stream_capture_outcomes = Counter()

        self.on_enroll_status = on_enroll_status
        self.on_verify_status = on_verify_status
        self.on_verify_finger_selected = on_verify_finger_selected

    # ------------------------------------------------------------------
    #  Sensor lifecycle
    # ------------------------------------------------------------------

    def prepare_sensor(self, reason, force=False, sensor=None):
        sensor = sensor if sensor is not None else self._sensor.session()
        try:
            if force:
                logger.info("Preparing sensor (%s): forced reconnect", reason)
                ok = sensor.force_reconnect(reset=reason.startswith("resume"))
                if ok:
                    ready = self._warm_sensor(reason, sensor)
                    self._set_sensor_readiness(sensor, ready)
                    return ready
                self._set_sensor_readiness(sensor, False)
                return False

            resume_ready = self._wait_for_resume_recovery(reason)
            if not resume_ready:
                return False
            logger.info("Preparing sensor (%s)", reason)
            if not sensor.ensure_connected():
                self._set_sensor_readiness(sensor, False)
                return False
            ok = sensor.refresh_after_idle()
            if ok:
                ready = self._warm_sensor(reason, sensor)
                self._set_sensor_readiness(sensor, ready)
                return ready
            self._set_sensor_readiness(sensor, False)
            return False
        except SensorCanceled:
            raise
        except Exception as e:
            self._set_sensor_readiness(sensor, False)
            logger.error("Sensor prepare failed (%s): %s", reason, e)
            return False

    def _set_sensor_readiness(self, sensor, ready):
        with self._operation_lock:
            if not sensor.active or self._closed:
                raise SensorCanceled("discarding stale sensor readiness")
            if ready:
                self._sensor_ready.set()
            else:
                self._sensor_ready.clear()

    def _wait_for_resume_recovery(self, reason):
        if self._resume_ready.is_set():
            return True

        logger.info(
            "Waiting up to %.1fs for resume recovery before %s",
            RESUME_RECOVERY_WAIT_SECONDS,
            reason,
        )
        if self._resume_ready.wait(timeout=RESUME_RECOVERY_WAIT_SECONDS):
            logger.info("Resume recovery completed before %s", reason)
            return True

        logger.warning("Resume recovery still pending before %s; deferring prepare", reason)
        return False

    def _warm_sensor(self, reason, sensor=None):
        result = self._capture.warm(
            RESUME_RECOVERY_WARMUP_FRAMES, read_timeout=250,
            backend=sensor if sensor is not None else self._sensor.session())
        logger.info(
            "[METRIC] component=readiness outcome=%s elapsed_ms=%.0f read_failures=%d",
            result.outcome.value,
            result.elapsed_ms,
            result.read_failures,
        )
        contrasts = result.details["contrasts"]
        valid_frames = result.details["valid_frames"]

        if contrasts:
            logger.info(
                "Sensor warmup (%s): valid=%d/%d contrast=%.1f/%.1f/%.1f ms=%.0f",
                reason,
                valid_frames,
                RESUME_RECOVERY_WARMUP_FRAMES,
                min(contrasts),
                sum(contrasts) / len(contrasts),
                max(contrasts),
                result.elapsed_ms,
            )
        else:
            logger.warning(
                "Sensor warmup (%s): no valid frames in %.0fms",
                reason,
                result.elapsed_ms,
            )
        return valid_frames > 0

    # ------------------------------------------------------------------
    #  Enrollment
    # ------------------------------------------------------------------

    def start_enroll(self, username, finger_name):
        self._discard_suspended_operation()
        self._stop_scan("new-enroll")
        time.sleep(0.1)

        self._enroll_scans = []
        self._enroll_touch_count = 0
        target_finger = finger_name if finger_name else "right-index-finger"
        self._start_scan(self._scan_loop, ("enroll", username, target_finger, True))

    # ------------------------------------------------------------------
    #  Verification
    # ------------------------------------------------------------------

    def start_verify(self, username, finger_name):
        self._discard_suspended_operation()
        self._verify_session_id += 1
        session_id = self._verify_session_id
        logger.info(
            "Verify session %d requested: user=%s raw_finger=%s "
            "resume_ready=%s recovery_running=%s",
            session_id,
            username,
            finger_name or "none",
            self._resume_ready.is_set(),
            self._resume_recovery_running,
        )

        target_finger = str(finger_name).strip() if finger_name else None
        if target_finger == "any":
            target_finger = None
        logger.info(
            "Verify session %d target: %s",
            session_id,
            target_finger or "any enrolled finger",
        )
        self._start_scan(
            self._scan_loop,
            ("verify", username, target_finger, True, session_id),
        )

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    def cancel(self):
        self._discard_suspended_operation()
        operation = self._stop_scan("cancel")
        if operation is None:
            return
        if operation.mode == "verify" and not operation.terminal:
            logger.info("Verify canceled; emitting terminal no-match")
            self._emit_verify("verify-no-match", True, operation, allow_inactive=True)
        elif operation.mode == "verify":
            logger.info("Verify canceled after terminal status; no extra status emitted")
        elif operation.mode == "enroll":
            logger.info("Enroll canceled; emitting terminal failure")
            self._emit_enroll("enroll-failed", True, operation, allow_inactive=True)

    def close(self):
        """Stop active work and release the out-of-process matcher."""
        self._closed = True
        self._resume_cancel.set()
        self._discard_suspended_operation()
        self._stop_scan("service-close")
        self._sensor.close()
        self._matcher_worker.close()
        if self._touch_matcher_worker is not None:
            self._touch_matcher_worker.close()

    def suspend(self):
        with self._resume_lock:
            self._resume_cancel.set()
            self._resume_generation += 1
        operation = self._stop_scan("suspend", suspend_sensor=True)
        scan_mode = operation.mode if operation else None
        logger.info("Service suspend: active_mode=%s", scan_mode)
        if operation and scan_mode == "verify" and not operation.terminal:
            with self._operation_lock:
                self._suspended_operation = operation
            logger.info("Pausing active verify for suspend")
            logger.info("Verify paused for suspend; no terminal status emitted")
        elif scan_mode == "enroll":
            logger.info("Enroll interrupted by suspend; emitting terminal failure")
            self._emit_enroll("enroll-failed", True, operation, allow_inactive=True)
        if not self._sensor.suspend(timeout=SCAN_STOP_TIMEOUT_SECONDS):
            logger.warning("USB release is pending behind an in-flight sensor command")

    def resume(self):
        logger.info("Service resume: starting recovery")
        self._sensor.resume()
        self._start_resume_recovery()
        with self._operation_lock:
            suspended_operation = self._suspended_operation
        if suspended_operation:
            logger.info("Scheduling suspended verify resume after recovery")
            threading.Thread(
                target=self._resume_suspended_scan_after_recovery,
                args=(suspended_operation, self._sensor.session()),
                name="egis-resume-scan",
                daemon=True,
            ).start()

    def _resume_suspended_scan_after_recovery(self, suspended_operation, sensor):
        while not self._resume_ready.wait(timeout=0.1):
            if not sensor.active or self._closed:
                return

        with self._operation_lock:
            if (self._suspended_operation is not suspended_operation or
                    not sensor.active or self._closed):
                logger.info("Suspended scan resume superseded")
                return
            self._suspended_operation = None
            scan_args = suspended_operation.args
            if scan_args and scan_args[0] == "verify":
                logger.info("Resuming suspended verify loop")
                self._start_scan(self._scan_loop, scan_args)

    def _start_resume_recovery(self):
        with self._resume_lock:
            self._resume_ready.clear()

            if self._resume_recovery_running and not self._resume_cancel.is_set():
                logger.info("Resume recovery already running")
                return

            self._resume_generation += 1
            generation = self._resume_generation
            self._resume_cancel = threading.Event()
            sensor = self._sensor.session(self._resume_cancel)
            self._resume_recovery_running = True
            self._resume_recovery_thread = threading.Thread(
                target=self._resume_recovery_loop,
                args=(generation, sensor, self._resume_cancel),
                name="egis-resume-recovery",
                daemon=True,
            )
            self._resume_recovery_thread.start()

    def _resume_recovery_loop(self, generation, sensor, cancel_event):
        start = time.time()
        ok = False
        try:
            if generation != self._resume_generation:
                logger.info("Superseding stale resume recovery generation %d", generation)
                return

            # Let the USB subsystem stabilize before attempting reconnect.
            # After a long sleep the bus and device may need a few seconds to
            # re-enumerate fully.
            delay = RESUME_USB_SETTLE_SECONDS
            logger.info("Resume recovery: waiting %.1fs for USB stabilization", delay)
            if cancel_event.wait(delay):
                return

            if generation != self._resume_generation:
                logger.info("Superseding stale resume recovery generation %d", generation)
                return

            logger.info("Resume recovery: reconnecting sensor")
            ok = self.prepare_sensor("resume-recovery", force=True, sensor=sensor)
        except SensorCanceled:
            logger.info("Resume recovery canceled")
        finally:
            elapsed_ms = (time.time() - start) * 1000.0
            with self._resume_lock:
                if generation == self._resume_generation and not self._closed:
                    self._resume_recovery_running = False
                    self._resume_ready.set()

            if cancel_event.is_set() or generation != self._resume_generation:
                logger.info("Resume recovery superseded")
            elif ok:
                logger.info("Resume recovery ready in %.0fms", elapsed_ms)
            else:
                logger.warning("Resume recovery failed after %.0fms", elapsed_ms)

    def list_enrolled_fingers(self, username):
        return self._matcher.get_enrolled_fingers(username)

    def delete_enrolled_fingers(self, username):
        self.cancel()
        self._matcher.delete_user_fingers(username)
        self._matcher_worker.reload()

    def diagnostics_snapshot(self):
        """Return aggregate reliability counters without biometric content."""
        capture = Counter(self._capture.metrics_snapshot())
        capture.update(self._stream_capture_outcomes)
        return {
            "capture": dict(capture),
            "matching": dict(self._match_outcomes),
            "sensor_ready": self._sensor_ready.is_set(),
            "resume_recovery_complete": self._resume_ready.is_set(),
        }

    # ------------------------------------------------------------------
    #  Thread management (private)
    # ------------------------------------------------------------------

    def _discard_suspended_operation(self):
        with self._operation_lock:
            if self._suspended_operation is not None:
                logger.info(
                    "Discarding suspended scan generation %d",
                    self._suspended_operation.generation,
                )
            self._suspended_operation = None

    def _is_operation_active(self, operation):
        with self._operation_lock:
            return (
                self._active_operation is operation and
                not operation.cancel_event.is_set()
            )

    def _finish_operation(self, operation):
        operation.cancel_event.set()
        with self._operation_lock:
            if self._active_operation is operation:
                self._active_operation = None

    def _stop_scan(self, reason="stop", suspend_sensor=False):
        with self._operation_lock:
            operation = self._active_operation
            if suspend_sensor:
                self._resume_ready.clear()
                self._sensor_ready.clear()
                self._sensor.suspend(timeout=0)
            if operation is None:
                return None
            logger.info(
                "Stopping %s scan generation %d (%s)...",
                operation.mode,
                operation.generation,
                reason,
            )
            operation.cancel_event.set()
            self._active_operation = None

        thread = operation.thread
        if thread and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=SCAN_STOP_TIMEOUT_SECONDS)
            if thread.is_alive():
                logger.warning(
                    "Scan generation %d did not exit cleanly; it remains canceled",
                    operation.generation,
                )
            else:
                logger.info("Thread stopped.")
        return operation

    def _start_scan(self, target_func, args):
        self._stop_scan("replacement")
        with self._operation_lock:
            if self._closed:
                raise RuntimeError("service is closed")
            self._operation_generation += 1
            operation = ScanOperation(self._operation_generation, args)
            operation.sensor = self._sensor.session(operation.cancel_event)
            thread = threading.Thread(
                target=self._run_scan,
                args=(target_func, operation, args),
                name=f"egis-{operation.mode}-{operation.generation}",
                daemon=True,
            )
            operation.thread = thread
            self._active_operation = operation
            self._scan_thread = thread
            thread.start()
        return operation

    def _run_scan(self, target, operation, args):
        try:
            target(operation, *args)
        except SensorCanceled:
            logger.info("Sensor session canceled for scan generation %d", operation.generation)
        finally:
            self._finish_operation(operation)

    def _operation_sensor(self, operation):
        if operation.sensor is None:
            operation.sensor = self._sensor.session(operation.cancel_event)
        return operation.sensor

    def _format_float(self, value):
        if value is None:
            return "none"
        return f"{float(value):.1f}"

    def _is_verify_touch(self, contrast, baseline_contrast):
        return self._capture.is_verify_touch(
            contrast,
            baseline_contrast,
            VERIFY_PRESENCE_THRESHOLD,
            VERIFY_PRESENCE_DELTA,
        )

    def _begin_touch_matching(self, operation, username, finger_name):
        if self._touch_matcher_worker is None:
            self._match_outcomes["touch:unavailable"] += 1
            return False
        try:
            available = self._touch_matcher_worker.begin(
                operation.generation, username, finger_name)
        except (BrokenPipeError, EOFError, OSError, RuntimeError, ValueError,
                TypeError) as error:
            logger.warning("Touch matcher session unavailable: %s", error)
            available = False
        self._match_outcomes[
            "touch:ready" if available else "touch:unavailable"] += 1
        return available

    def _touch_discontinuity(self):
        try:
            self._touch_matcher_worker.discontinuity()
            return True
        except (BrokenPipeError, EOFError, OSError, RuntimeError, ValueError,
                TypeError) as error:
            logger.warning("Touch matcher discontinuity failed: %s", error)
            self._match_outcomes["touch:worker_failure"] += 1
            return False

    def _observe_touch(self, message):
        try:
            return self._touch_matcher_worker.observe(
                message.pixels, message.sequence)
        except (BrokenPipeError, EOFError, OSError, RuntimeError, ValueError,
                TypeError) as error:
            logger.warning("Touch matcher observation failed: %s", error)
            self._match_outcomes["touch:worker_failure"] += 1
            return None

    def _record_touch_decision(self, decision):
        logger.info(
            "[SHADOW] component=touch_matching accepted=%s identity=%s "
            "reason=%s score=%.3f margin=%.3f cells=%d frames=%d",
            decision.accepted,
            decision.identity or "none",
            decision.reason,
            decision.score,
            decision.margin,
            decision.metrics["best"]["supported_cells"],
            decision.metrics["best"]["admitted_frames"],
        )
        self._match_outcomes[
            "touch:accept" if decision.accepted
            else f"touch:reject:{decision.reason}"] += 1

    def _end_touch_matching(self):
        if self._touch_matcher_worker is None:
            return
        try:
            self._touch_matcher_worker.end()
        except (BrokenPipeError, EOFError, OSError, RuntimeError, ValueError,
                TypeError) as error:
            logger.warning("Touch matcher cleanup failed: %s", error)
            self._match_outcomes["touch:worker_failure"] += 1

    # ------------------------------------------------------------------
    #  Scan loop
    # ------------------------------------------------------------------

    def _wait_for_finger_release(self, operation):
        logger.info("Waiting for finger release...")
        time.sleep(0.3)
        consecutive_clears = 0
        while self._is_operation_active(operation):
            frame, _, is_present = self._operation_sensor(operation).capture_presence_frame()
            if frame is None:
                consecutive_clears = 0
                operation.cancel_event.wait(0.1)
                continue
            if not is_present:
                consecutive_clears += 1
                if consecutive_clears >= 2:
                    logger.info("Sensor clear. Ready.")
                    return
            else:
                consecutive_clears = 0
            time.sleep(0.1)

    def _scan_loop(self, operation, mode, username, finger_name,
                   prepare_before_loop=True, session_id=None):
        session_label = session_id if session_id is not None else "none"
        logger.info(
            "Starting %s loop session=%s user=%s finger=%s prepare_before_loop=%s",
            mode,
            session_label,
            username,
            finger_name,
            prepare_before_loop,
        )
        if prepare_before_loop:
            while self._is_operation_active(operation):
                if self.prepare_sensor(
                        f"{mode}-loop", sensor=self._operation_sensor(operation)):
                    break
                operation.cancel_event.wait(0.25)
        if not self._is_operation_active(operation):
            return
        self._wait_for_finger_release(operation)
        empty_frames = 0
        no_touch_since = time.time()
        last_idle_recovery = no_touch_since
        no_touch_contrasts = []
        baseline_samples = []
        baseline_contrast = None

        while self._is_operation_active(operation):
            sensor = self._operation_sensor(operation)
            img, contrast, is_present = sensor.capture_presence_frame()
            touch_reason = "driver-threshold" if is_present else "none"
            if mode == "verify" and img is not None:
                if baseline_contrast is None:
                    baseline_samples.append(float(contrast))
                    if len(baseline_samples) >= VERIFY_BASELINE_SAMPLE_COUNT:
                        baseline_contrast = sum(baseline_samples) / len(baseline_samples)
                        logger.info(
                            "Verify baseline contrast established: baseline=%.1f "
                            "samples=%d session=%s threshold=%.1f verify_threshold=%.1f "
                            "delta=%.1f",
                            baseline_contrast,
                            len(baseline_samples),
                            session_label,
                            sensor.touch_threshold,
                            VERIFY_PRESENCE_THRESHOLD,
                            VERIFY_PRESENCE_DELTA,
                        )
                is_present, touch_reason = self._is_verify_touch(
                    float(contrast), baseline_contrast)

            if img is None:
                empty_frames += 1
                if empty_frames >= 10:
                    logger.warning("No sensor frames; forcing USB recovery...")
                    sensor.force_reconnect(reset=True)
                    empty_frames = 0
            else:
                empty_frames = 0

            if is_present:
                if no_touch_contrasts:
                    logger.info(
                        "Touch detected after no-touch window: reason=%s contrast_now=%.1f "
                        "window_avg=%.1f window_max=%.1f samples=%d session=%s "
                        "baseline=%s",
                        touch_reason,
                        contrast,
                        sum(no_touch_contrasts) / len(no_touch_contrasts),
                        max(no_touch_contrasts),
                        len(no_touch_contrasts),
                        session_label,
                        self._format_float(baseline_contrast),
                    )
                    no_touch_contrasts = []
                no_touch_since = time.time()
                logger.info("Finger detected!")

                if mode == "enroll":
                    if img is not None:
                        logger.info("Captured frame. Contrast: %.2f", contrast)
                        self._handle_enroll(operation, img, username, finger_name)
                elif mode == "verify":
                    self._handle_verify_continuous(
                        operation, username, finger_name, img, contrast)
                    no_touch_since = time.time()
            elif mode == "verify":
                if img is not None:
                    no_touch_contrasts.append(float(contrast))
                    if len(no_touch_contrasts) > 200:
                        no_touch_contrasts = no_touch_contrasts[-200:]
                now = time.time()
                # An armed sensor is not an authentication attempt. Hyprlock may
                # leave verification running while the display is off, so only a
                # real touch may produce a match or retry result.
                if (
                        now - no_touch_since >= VERIFY_IDLE_LOG_INTERVAL_SECONDS and
                        now - last_idle_recovery >= VERIFY_IDLE_LOG_INTERVAL_SECONDS):
                    if no_touch_contrasts:
                        logger.info(
                            "Verify no-touch contrast window: avg=%.1f max=%.1f "
                            "samples=%d threshold=%.1f session=%s",
                            sum(no_touch_contrasts) / len(no_touch_contrasts),
                            max(no_touch_contrasts),
                            len(no_touch_contrasts),
                            sensor.touch_threshold,
                            session_label,
                        )
                    else:
                        logger.warning("Verify no-touch window had no valid frames")
                    logger.info(
                        "Verify armed but no touch detected; leaving USB stable "
                        "(empty_frames=%d baseline=%s)",
                        empty_frames,
                        self._format_float(baseline_contrast),
                    )
                    last_idle_recovery = now
                    no_touch_since = now
                    no_touch_contrasts = []

            time.sleep(0.05)

    # ------------------------------------------------------------------
    #  Enroll logic
    # ------------------------------------------------------------------

    def _handle_enroll(self, operation, img, username, finger_name):
        time.sleep(0.05)
        capture = self._capture.collect_touch(
            lambda: self._is_operation_active(operation), img,
            backend=self._operation_sensor(operation))
        logger.info(
            "[METRIC] component=enrollment_capture outcome=%s frames=%d "
            "elapsed_ms=%.0f read_failures=%d",
            capture.outcome.value,
            len(capture.frames),
            capture.elapsed_ms,
            capture.read_failures,
        )
        if capture.outcome is CaptureOutcome.CANCELED:
            return
        touch_frames = list(capture.frames)
        capture_ms = capture.elapsed_ms
        analysis = self._matcher.analyze_touch(touch_frames)
        logger.info(
            "Enroll touch summary: frames=%d usable=%d contrast=%.1f/%.1f/%.1f "
            "quality=%.3f max_kp=%d accepted=%s capture_ms=%.0f",
            analysis["frames"],
            analysis["usable_frames"],
            analysis["contrast_min"],
            analysis["contrast_avg"],
            analysis["contrast_max"],
            analysis["quality_avg"],
            analysis["max_keypoints"],
            analysis["usable"],
            capture_ms,
        )

        if not self._is_operation_active(operation):
            logger.info(
                "Discarding enroll analysis from stale generation %d",
                operation.generation,
            )
            return

        if not analysis["usable"]:
            logger.info("Enrollment touch too weak; retrying same stage.")
            self._emit_enroll("enroll-retry-scan", False, operation)
            if self._is_operation_active(operation):
                self._wait_for_finger_release(operation)
            return

        self._enroll_scans.append(touch_frames)
        self._enroll_touch_count += 1

        count = self._enroll_touch_count
        target = ENROLL_STAGES

        total_frames = sum(len(frames) for frames in self._enroll_scans)
        logger.info("Enroll Progress: %d/%d (touch: %d frames, total: %d)",
                     count, target, len(touch_frames), total_frames)

        if count < target:
            self._emit_enroll("enroll-stage-passed", False, operation)
        else:
            unique_name = f"{username}_{finger_name}"
            logger.info("Processing enrollment for %s (%d total frames)...",
                         unique_name, total_frames)
            success = self._matcher.enroll_finger(unique_name, self._enroll_scans)

            if success:
                self._matcher_worker.reload()
                logger.info("Enrollment Successful!")
                self._emit_enroll("enroll-completed", True, operation)
            else:
                logger.error("Enrollment Failed")
                self._emit_enroll("enroll-failed", True, operation)

            self._finish_operation(operation)
            self._enroll_touch_count = 0

        if self._is_operation_active(operation):
            self._wait_for_finger_release(operation)

    # ------------------------------------------------------------------
    #  Verify logic
    # ------------------------------------------------------------------

    def _handle_verify_continuous(self, operation, username, finger_name,
                                   initial_img=None, initial_contrast=0.0):
        logger.info("Continuous verify - trying while finger is on sensor...")
        attempt = 0
        confirmation = ConfirmationTracker(self._confirmation_policy)
        self._capture_epoch += 1
        capture_epoch = self._capture_epoch
        sensor = self._operation_sensor(operation)
        frame_spec = sensor.frame_spec
        pump = CapturePump(
            sensor,
            frame_spec,
            operation.generation,
            capture_epoch,
            initial_img,
            initial_contrast,
        ).start()
        touch_available = self._begin_touch_matching(
            operation, username, finger_name)
        frames = []
        window_started = None
        contact_deadline = time.monotonic() + 3.0
        try:
            while (
                    self._is_operation_active(operation) and
                    time.monotonic() < contact_deadline):
                try:
                    message = pump.stream.receive(timeout=0.1)
                except queue.Empty:
                    if not pump.alive:
                        break
                    continue
                if (
                        message.generation != operation.generation or
                        message.capture_epoch != capture_epoch):
                    continue
                if message.dropped_before:
                    self._stream_capture_outcomes["dropped_frames"] += message.dropped_before
                    logger.info(
                        "[METRIC] component=verification_capture "
                        "outcome=queue_overflow dropped_frames=%d",
                        message.dropped_before)
                    logger.warning(
                        "Capture queue dropped %d frame(s); resetting evidence",
                        message.dropped_before,
                    )
                    frames = []
                    window_started = None
                    confirmation.reset()
                    if touch_available:
                        touch_available = self._touch_discontinuity()
                if message.status is FrameStatus.CONTACT_END:
                    break
                if message.status is FrameStatus.DEVICE_UNAVAILABLE:
                    self._stream_capture_outcomes["device_unavailable"] += 1
                    self._set_sensor_readiness(sensor, False)
                    logger.info(
                        "[METRIC] component=verification_capture "
                        "outcome=device_unavailable frames=0")
                    break
                if message.status is FrameStatus.IO_ERROR:
                    frames = []
                    window_started = None
                    confirmation.reset()
                    if touch_available:
                        touch_available = self._touch_discontinuity()
                    self._stream_capture_outcomes["io_error"] += 1
                    logger.info(
                        "[METRIC] component=verification_capture outcome=io_error "
                        "frames=0 observed_bytes=%d",
                        message.observed_bytes,
                    )
                    continue
                if message.status is not FrameStatus.VALID:
                    continue

                # Feed continuous evidence before the legacy fixed-window path.
                # The worker keeps at most one frame in flight, so capture can
                # never accumulate an unbounded matching backlog.
                touch_decision = (
                    self._observe_touch(message) if touch_available else None)
                if touch_decision is not None:
                    self._record_touch_decision(touch_decision)
                if self._match_mode == "touch":
                    if (touch_decision is not None and touch_decision.accepted and
                            touch_decision.identity and
                            touch_decision.identity.startswith(username + "_")):
                        logger.info("AUTHENTICATED by touch evidence")
                        self._emit_verify("verify-match", True, operation)
                        self._finish_operation(operation)
                        return
                    continue

                if not frames:
                    window_started = message.captured_started
                frames.append(message.pixels)
                if len(frames) < self._confirmation_policy.frames_per_attempt:
                    continue

                attempt += 1
                match_start = time.monotonic()
                queue_age_ms = max(
                    0.0, (match_start - message.captured_finished) * 1000.0)
                decision = self._matcher_worker.evaluate(
                    frames,
                    username,
                    finger_name,
                    operation.generation,
                )
                match_ms = (time.monotonic() - match_start) * 1000.0
                capture_ms = (
                    message.captured_finished - window_started) * 1000.0
                logger.info(
                    "[METRIC] component=verification_capture outcome=captured "
                    "frames=%d elapsed_ms=%.0f dropped_before=%d queue_age_ms=%.0f",
                    len(frames),
                    capture_ms,
                    message.dropped_before,
                    queue_age_ms,
                )
                self._stream_capture_outcomes["captured"] += 1
                frames = []
                window_started = None
                if not self._is_operation_active(operation):
                    break
                if self._apply_match_decision(
                        operation, username, decision, confirmation,
                        attempt, capture_ms, match_ms):
                    return
        finally:
            pump.stop()
            self._end_touch_matching()

        logger.info("No match after %d attempts", attempt)
        if self._is_operation_active(operation):
            self._emit_verify("verify-retry-scan", False, operation)
            logger.info("Ready for another verification touch.")
        else:
            logger.info("Verification loop stopped before retry emission.")

    def _apply_match_decision(self, operation, username, decision, confirmation,
                              attempt, capture_ms, match_ms):
        match_name, score = decision.as_legacy_result()
        stats = decision.metrics
        self._match_outcomes[decision.outcome.value] += 1
        if decision.reason:
            self._match_outcomes[f"reason:{decision.reason}"] += 1
        logger.info(
            "[METRIC] component=matching outcome=%s reason=%s elapsed_ms=%.0f",
            decision.outcome.value,
            decision.reason or "none",
            match_ms,
        )
        best = stats.get("best", {})
        logger.info(
            "Verify attempt summary: attempt=%d frames=%d keypoints=%d good=%d "
            "candidates=%d best_inliers=%d ridge=%.2f ncc=%.2f orient=%.2f "
            "calibrated=%s match=%s reject=%s capture_match_ms=%.0f/%.0f",
            attempt,
            self._confirmation_policy.frames_per_attempt,
            stats.get("keypoints", 0),
            stats.get("good_matches", 0),
            stats.get("candidates", 0),
            stats.get("best_inliers", 0),
            best.get("ridge_score", 0.0),
            best.get("ncc", 0.0),
            best.get("orientation", 0.0),
            stats.get("calibrated", False),
            match_name if match_name else "none",
            stats.get("reject_reason", "none"),
            capture_ms,
            match_ms,
        )
        if not match_name:
            confirmation.record(False)
            return False
        if not match_name.startswith(username + "_"):
            confirmation.reset()
            return False
        name_rest = match_name[len(username) + 1:]
        if "_" in name_rest:
            confirmation.reset()
            return False
        confirmed = confirmation.record(True, identity=name_rest)
        if confirmation.last_reset_reason == "identity_switch":
            self._match_outcomes["reason:identity_switch"] += 1
            logger.warning(
                "Verification identity switch: previous=%s current=%s; "
                "confirmation reset",
                confirmation.previous_identity,
                name_rest,
            )
        logger.info(
            "Verification confirmation identity=%s %d/%d",
            confirmation.identity,
            confirmation.consecutive_accepts,
            self._confirmation_policy.required_consecutive_accepts,
        )
        if not confirmed:
            return False
        logger.info("AUTHENTICATED!")
        self._emit_verify("verify-match", True, operation)
        self._finish_operation(operation)
        return True

    # ------------------------------------------------------------------
    #  Callback emission (private)
    # ------------------------------------------------------------------

    def _emit_enroll(self, result, done, operation, allow_inactive=False):
        with self._operation_lock:
            if (
                    not allow_inactive and
                    (
                        self._active_operation is not operation or
                        operation.cancel_event.is_set()
                    )):
                logger.info(
                    "Dropping stale enroll status from generation %d: %s",
                    operation.generation,
                    result,
                )
                return False
            if done:
                operation.terminal = True
        if self.on_enroll_status:
            self.on_enroll_status(result, done)
        return True

    def _emit_verify(self, result, done, operation, allow_inactive=False):
        with self._operation_lock:
            if (
                    not allow_inactive and
                    (
                        self._active_operation is not operation or
                        operation.cancel_event.is_set()
                    )):
                logger.info(
                    "Dropping stale verify status from generation %d: %s",
                    operation.generation,
                    result,
                )
                return False
            if done:
                operation.terminal = True
        if self.on_verify_status:
            self.on_verify_status(result, done)
        return True
