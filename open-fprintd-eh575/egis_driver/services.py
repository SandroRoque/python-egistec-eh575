import logging
import threading
import time

from egis_driver import egis_driver, fingerprint_matcher
from egis_driver.persistence import Persistence

logger = logging.getLogger("SERVICE")

ENROLL_STAGES = 10
VERIFY_FRAME_COUNT = 3
VERIFY_CONFIRMATION_ATTEMPTS = 2
RESUME_RECOVERY_WAIT_SECONDS = 3.0
RESUME_RECOVERY_WARMUP_FRAMES = 4
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


class EgisService:
    """Core domain logic for the Egis EH575 fingerprint sensor.

    Handles enrollment, verification, sensor health, and scan loops.
    Communicates results via callbacks — has zero knowledge of D-Bus.
    """

    def __init__(self, driver=None, matcher=None, persistence=None,
                 on_enroll_status=None, on_verify_status=None,
                 on_verify_finger_selected=None):
        self._driver = driver or egis_driver.EgisDriver()
        self._persistence = persistence or Persistence("/var/lib/open-fprintd")
        self._matcher = matcher or fingerprint_matcher.FingerprintMatcher(
            persistence=self._persistence)

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
        self._resume_recovery_thread = None
        self._resume_recovery_running = False
        self._resume_generation = 0
        self._verify_session_id = 0

        self.on_enroll_status = on_enroll_status
        self.on_verify_status = on_verify_status
        self.on_verify_finger_selected = on_verify_finger_selected

    # ------------------------------------------------------------------
    #  Sensor lifecycle
    # ------------------------------------------------------------------

    def prepare_sensor(self, reason, force=False):
        try:
            if force:
                logger.info("Preparing sensor (%s): forced reconnect", reason)
                ok = self._driver.force_reconnect(reset=reason.startswith("resume"))
                if ok:
                    self._warm_sensor(reason)
                return ok

            resume_ready = self._wait_for_resume_recovery(reason)
            logger.info("Preparing sensor (%s)", reason)
            if not self._driver._ensure_connected(force=not resume_ready):
                return False
            ok = self._driver.refresh_after_idle()
            if ok:
                self._warm_sensor(reason)
            return ok
        except Exception as e:
            logger.error("Sensor prepare failed (%s): %s", reason, e)
            return False

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

        logger.warning("Resume recovery still pending before %s; continuing with prepare", reason)
        return False

    def _warm_sensor(self, reason):
        valid_frames = 0
        contrasts = []
        start = time.time()
        for _ in range(RESUME_RECOVERY_WARMUP_FRAMES):
            img, contrast, _ = self._driver.capture_presence_frame(read_timeout=250)
            if img is not None:
                valid_frames += 1
                contrasts.append(float(contrast))
            time.sleep(0.03)

        if contrasts:
            logger.info(
                "Sensor warmup (%s): valid=%d/%d contrast=%.1f/%.1f/%.1f ms=%.0f",
                reason,
                valid_frames,
                RESUME_RECOVERY_WARMUP_FRAMES,
                min(contrasts),
                sum(contrasts) / len(contrasts),
                max(contrasts),
                (time.time() - start) * 1000.0,
            )
        else:
            logger.warning(
                "Sensor warmup (%s): no valid frames in %.0fms",
                reason,
                (time.time() - start) * 1000.0,
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

    def suspend(self):
        operation = self._stop_scan("suspend")
        scan_mode = operation.mode if operation else None
        logger.info("Service suspend: active_mode=%s", scan_mode)
        self._resume_ready.clear()
        if operation and scan_mode == "verify" and not operation.terminal:
            with self._operation_lock:
                self._suspended_operation = operation
            logger.info("Pausing active verify for suspend")
            logger.info("Verify paused for suspend; no terminal status emitted")
        elif scan_mode == "enroll":
            logger.info("Enroll interrupted by suspend; emitting terminal failure")
            self._emit_enroll("enroll-failed", True, operation, allow_inactive=True)
        self._driver.release_for_sleep()

    def resume(self):
        logger.info("Service resume: starting recovery")
        self._start_resume_recovery()
        with self._operation_lock:
            suspended_operation = self._suspended_operation
        if suspended_operation:
            logger.info("Scheduling suspended verify resume after recovery")
            threading.Thread(
                target=self._resume_suspended_scan_after_recovery,
                args=(suspended_operation,),
                name="egis-resume-scan",
                daemon=True,
            ).start()

    def _resume_suspended_scan_after_recovery(self, suspended_operation):
        if not self._resume_ready.wait(timeout=RESUME_RECOVERY_WAIT_SECONDS + 5.0):
            logger.warning("Resume recovery did not finish before suspended scan restart")

        with self._operation_lock:
            if self._suspended_operation is not suspended_operation:
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

            if self._resume_recovery_running:
                logger.info("Resume recovery already running")
                return

            self._resume_generation += 1
            generation = self._resume_generation
            self._resume_recovery_running = True
            self._resume_recovery_thread = threading.Thread(
                target=self._resume_recovery_loop,
                args=(generation,),
                name="egis-resume-recovery",
                daemon=True,
            )
            self._resume_recovery_thread.start()

    def _resume_recovery_loop(self, generation):
        start = time.time()
        ok = False
        try:
            if generation != self._resume_generation:
                logger.info("Superseding stale resume recovery generation %d", generation)
                return

            # Let the USB subsystem stabilize before attempting reconnect.
            # After a long sleep the bus and device may need a few seconds to
            # re-enumerate fully.
            delay = 2.0
            logger.info("Resume recovery: waiting %.1fs for USB stabilization", delay)
            time.sleep(delay)

            if generation != self._resume_generation:
                logger.info("Superseding stale resume recovery generation %d", generation)
                return

            logger.info("Resume recovery: reconnecting sensor")
            ok = self.prepare_sensor("resume-recovery", force=True)
        finally:
            elapsed_ms = (time.time() - start) * 1000.0
            with self._resume_lock:
                self._resume_recovery_running = False
                self._resume_ready.set()

            if ok:
                logger.info("Resume recovery ready in %.0fms", elapsed_ms)
            else:
                logger.warning("Resume recovery failed after %.0fms", elapsed_ms)

    def list_enrolled_fingers(self, username):
        return self._matcher.get_enrolled_fingers(username)

    def delete_enrolled_fingers(self, username):
        self._matcher.delete_user_fingers(username)

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

    def _stop_scan(self, reason="stop"):
        with self._operation_lock:
            operation = self._active_operation
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
            self._operation_generation += 1
            operation = ScanOperation(self._operation_generation, args)
            thread = threading.Thread(
                target=target_func,
                args=(operation, *args),
                name=f"egis-{operation.mode}-{operation.generation}",
                daemon=True,
            )
            operation.thread = thread
            self._active_operation = operation
            self._scan_thread = thread
            thread.start()
        return operation

    def _format_float(self, value):
        if value is None:
            return "none"
        return f"{float(value):.1f}"

    def _is_verify_touch(self, contrast, baseline_contrast):
        if contrast >= self._driver.touch_threshold:
            return True, "driver-threshold"
        if contrast >= VERIFY_PRESENCE_THRESHOLD:
            return True, "verify-threshold"
        if (
                baseline_contrast is not None and
                contrast >= baseline_contrast + VERIFY_PRESENCE_DELTA):
            return True, "baseline-delta"
        return False, "none"

    # ------------------------------------------------------------------
    #  Scan loop
    # ------------------------------------------------------------------

    def _wait_for_finger_release(self, operation):
        logger.info("Waiting for finger release...")
        time.sleep(0.3)
        consecutive_clears = 0
        while self._is_operation_active(operation):
            _, _, is_present = self._driver.capture_presence_frame()
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
            self.prepare_sensor(f"{mode}-loop")
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
            img, contrast, is_present = self._driver.capture_presence_frame()
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
                            self._driver.touch_threshold,
                            VERIFY_PRESENCE_THRESHOLD,
                            VERIFY_PRESENCE_DELTA,
                        )
                is_present, touch_reason = self._is_verify_touch(
                    float(contrast), baseline_contrast)

            if img is None:
                empty_frames += 1
                if empty_frames >= 10:
                    logger.warning("No sensor frames; forcing USB recovery...")
                    self._driver.force_reconnect(reset=True)
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
                            self._driver.touch_threshold,
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

        touch_frames = [img]
        start = time.time()
        capture_start = start
        max_duration = 3.0

        while (
                time.time() - start < max_duration and
                self._is_operation_active(operation)):
            extra_img, contrast = self._driver.get_live_frame()
            if extra_img is None or contrast < 15:
                break
            touch_frames.append(extra_img)

        capture_ms = (time.time() - capture_start) * 1000.0
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
        consecutive_matches = 0
        low_contrast_streak = 0
        pending_img = initial_img
        pending_contrast = initial_contrast

        while self._is_operation_active(operation):
            capture_start = time.time()
            frames = []
            if pending_img is not None and pending_contrast >= 15:
                frames.append(pending_img)
            pending_img = None
            pending_contrast = 0.0

            while (
                    len(frames) < VERIFY_FRAME_COUNT and
                    self._is_operation_active(operation)):
                extra_img, contrast = self._driver.get_live_frame()
                if extra_img is None or contrast < 15:
                    break
                frames.append(extra_img)
                time.sleep(0.01)

            if len(frames) < VERIFY_FRAME_COUNT:
                low_contrast_streak += 1
                if low_contrast_streak >= 10:
                    logger.info("Finger lifted")
                    break
                time.sleep(0.05)
                continue

            low_contrast_streak = 0
            attempt += 1

            match_start = time.time()
            match_name, score = self._matcher.verify_finger_multiframe(
                frames,
                username=username,
                finger_name=finger_name,
            )
            match_ms = (time.time() - match_start) * 1000.0
            total_ms = (time.time() - capture_start) * 1000.0
            stats = getattr(self._matcher, "last_verify_stats", {})
            best = stats.get("best", {})
            logger.info(
                "Verify attempt summary: attempt=%d frames=%d keypoints=%d "
                "good=%d candidates=%d best_inliers=%d ridge=%.2f ncc=%.2f orient=%.2f "
                "calibrated=%s match=%s reject=%s capture_match_ms=%.0f/%.0f",
                attempt,
                len(frames),
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
                total_ms - match_ms,
                match_ms,
            )

            if match_name:
                logger.info("Match: %s (Inliers: %s, Frames: %d)",
                             match_name, score, len(frames))
                if match_name.startswith(username + "_"):
                    name_rest = match_name[len(username) + 1:]
                    if "_" not in name_rest:
                        consecutive_matches += 1
                        logger.info(
                            "Verification confirmation %d/%d",
                            consecutive_matches,
                            VERIFY_CONFIRMATION_ATTEMPTS,
                        )
                        if consecutive_matches >= VERIFY_CONFIRMATION_ATTEMPTS:
                            logger.info("AUTHENTICATED!")
                            self._emit_verify("verify-match", True, operation)
                            self._finish_operation(operation)
                            return
                    else:
                        logger.info("Username collision: %s is not %s",
                                    match_name, username)
                        consecutive_matches = 0
                else:
                    logger.info("Wrong user! (%s)", match_name)
                    consecutive_matches = 0
            else:
                consecutive_matches = 0

            time.sleep(0.05)

        logger.info("No match after %d attempts", attempt)
        if self._is_operation_active(operation):
            self._emit_verify("verify-retry-scan", False, operation)
            logger.info("Ready for another verification touch.")
        else:
            logger.info("Verification loop stopped before retry emission.")

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
