import logging
import threading
import time

from egis_driver import egis_driver, fingerprint_matcher
from egis_driver.persistence import Persistence

logger = logging.getLogger("SERVICE")

ENROLL_STAGES = 10
VERIFY_FRAME_COUNT = 3
RESUME_RECOVERY_WAIT_SECONDS = 2.5
RESUME_RECOVERY_MAX_ATTEMPTS = 3
RESUME_RECOVERY_WARMUP_FRAMES = 4


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

        self._scanning = False
        self._scan_mode = None
        self._scan_thread = None
        self._enroll_scans = []
        self._enroll_touch_count = 0
        self._last_prepare_reason = None
        self._resume_lock = threading.Lock()
        self._resume_ready = threading.Event()
        self._resume_ready.set()
        self._resume_recovery_thread = None
        self._resume_recovery_running = False
        self._resume_generation = 0
        self._last_resume_result = None

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
                ok = self._driver.force_reconnect()
                if ok:
                    self._warm_sensor(reason)
                if ok:
                    self._last_prepare_reason = reason
                return ok

            resume_ready = self._wait_for_resume_recovery(reason)
            logger.info("Preparing sensor (%s)", reason)
            if not self._driver._ensure_connected(force=not resume_ready):
                return False
            ok = self._driver.refresh_after_idle()
            if ok:
                self._warm_sensor(reason)
            if ok:
                self._last_prepare_reason = reason
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
        self._stop_scan()
        time.sleep(0.1)
        self.prepare_sensor("enroll-start")

        self._enroll_scans = []
        self._enroll_touch_count = 0
        target_finger = finger_name if finger_name else "right-index-finger"
        self._start_scan(self._scan_loop, ("enroll", username, target_finger, False))

    # ------------------------------------------------------------------
    #  Verification
    # ------------------------------------------------------------------

    def start_verify(self, username, finger_name):
        prepared = self.prepare_sensor("verify-start")
        target_finger = str(finger_name).strip() if finger_name else None
        if target_finger == "any":
            target_finger = None
        logger.info("Verify target: %s", target_finger or "any enrolled finger")
        self._start_scan(self._scan_loop, ("verify", username, target_finger, not prepared))

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    def cancel(self):
        scan_mode = self._scan_mode
        self._stop_scan()
        if scan_mode == "verify":
            logger.info("Verify canceled; emitting terminal no-match")
            self._emit_verify("verify-no-match", True)
        elif scan_mode == "enroll":
            logger.info("Enroll canceled; emitting terminal failure")
            self._emit_enroll("enroll-failed", True)

    def suspend(self):
        self._resume_ready.clear()
        self._stop_scan()
        self._driver.release_for_sleep()

    def resume(self):
        self._start_resume_recovery()

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
            for attempt in range(1, RESUME_RECOVERY_MAX_ATTEMPTS + 1):
                if generation != self._resume_generation:
                    logger.info("Superseding stale resume recovery generation %d", generation)
                    return

                logger.info(
                    "Resume recovery attempt %d/%d",
                    attempt,
                    RESUME_RECOVERY_MAX_ATTEMPTS,
                )
                ok = self.prepare_sensor(f"resume-attempt-{attempt}", force=True)
                if ok:
                    break
                time.sleep(min(0.5 * attempt, 1.5))
        finally:
            elapsed_ms = (time.time() - start) * 1000.0
            with self._resume_lock:
                self._last_resume_result = {
                    "ok": ok,
                    "generation": generation,
                    "elapsed_ms": elapsed_ms,
                }
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

    def _stop_scan(self):
        if self._scanning:
            logger.info("Stopping active scan...")
            self._scanning = False

        if self._scan_thread and self._scan_thread.is_alive():
            self._scan_thread.join(timeout=2.0)
            if self._scan_thread.is_alive():
                logger.warning("Thread did not exit cleanly!")
            else:
                logger.info("Thread stopped.")
        self._scan_mode = None

    def _start_scan(self, target_func, args):
        self._stop_scan()
        self._scanning = True
        self._scan_mode = args[0] if args else None
        self._scan_thread = threading.Thread(target=target_func, args=args)
        self._scan_thread.start()

    # ------------------------------------------------------------------
    #  Scan loop
    # ------------------------------------------------------------------

    def _wait_for_finger_release(self):
        logger.info("Waiting for finger release...")
        time.sleep(0.3)
        consecutive_clears = 0
        while self._scanning:
            _, _, is_present = self._driver.capture_presence_frame()
            if not is_present:
                consecutive_clears += 1
                if consecutive_clears >= 2:
                    logger.info("Sensor clear. Ready.")
                    return
            else:
                consecutive_clears = 0
            time.sleep(0.1)

    def _scan_loop(self, mode, username, finger_name, prepare_before_loop=True):
        logger.info("Starting %s loop for %s (%s)...", mode, username, finger_name)
        if prepare_before_loop:
            self.prepare_sensor(f"{mode}-loop")
        self._wait_for_finger_release()
        empty_frames = 0
        no_touch_since = time.time()
        last_idle_recovery = no_touch_since

        while self._scanning:
            img, contrast, is_present = self._driver.capture_presence_frame()
            if img is None:
                empty_frames += 1
                if empty_frames >= 10:
                    logger.warning("No sensor frames; forcing USB recovery...")
                    self._driver.force_reconnect()
                    empty_frames = 0
            else:
                empty_frames = 0

            if is_present:
                no_touch_since = time.time()
                logger.info("Finger detected!")

                if mode == "enroll":
                    if img is not None:
                        logger.info("Captured frame. Contrast: %.2f", contrast)
                        self._handle_enroll(img, username, finger_name)
                elif mode == "verify":
                    self._handle_verify_continuous(username, finger_name, img, contrast)
                    no_touch_since = time.time()
            elif mode == "verify":
                now = time.time()
                if now - no_touch_since >= 10.0 and now - last_idle_recovery >= 10.0:
                    logger.info("Verify armed but no touch detected; refreshing USB...")
                    self._driver.force_reconnect()
                    last_idle_recovery = now
                    no_touch_since = now
                    self._wait_for_finger_release()

            time.sleep(0.05)

    # ------------------------------------------------------------------
    #  Enroll logic
    # ------------------------------------------------------------------

    def _handle_enroll(self, img, username, finger_name):
        time.sleep(0.05)

        touch_frames = [img]
        start = time.time()
        capture_start = start
        max_duration = 3.0

        while time.time() - start < max_duration and self._scanning:
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

        if not analysis["usable"]:
            logger.info("Enrollment touch too weak; retrying same stage.")
            self._emit_enroll("enroll-retry-scan", False)
            if self._scanning:
                self._wait_for_finger_release()
            return

        self._enroll_scans.extend(touch_frames)
        self._enroll_touch_count += 1

        count = self._enroll_touch_count
        target = ENROLL_STAGES

        logger.info("Enroll Progress: %d/%d (touch: %d frames, total: %d)",
                     count, target, len(touch_frames), len(self._enroll_scans))

        if count < target:
            self._emit_enroll("enroll-stage-passed", False)
        else:
            unique_name = f"{username}_{finger_name}"
            logger.info("Processing enrollment for %s (%d total frames)...",
                         unique_name, len(self._enroll_scans))
            success = self._matcher.enroll_finger(unique_name, self._enroll_scans)

            if success:
                logger.info("Enrollment Successful!")
                self._emit_enroll("enroll-completed", True)
            else:
                logger.error("Enrollment Failed")
                self._emit_enroll("enroll-failed", True)

            self._scanning = False
            self._enroll_touch_count = 0

        if self._scanning:
            self._wait_for_finger_release()

    # ------------------------------------------------------------------
    #  Verify logic
    # ------------------------------------------------------------------

    def _handle_verify_continuous(self, username, finger_name,
                                   initial_img=None, initial_contrast=0.0):
        logger.info("Continuous verify - trying while finger is on sensor...")
        attempt = 0
        low_contrast_streak = 0
        pending_img = initial_img
        pending_contrast = initial_contrast

        while self._scanning:
            capture_start = time.time()
            frames = []
            if pending_img is not None and pending_contrast >= 15:
                frames.append(pending_img)
            pending_img = None
            pending_contrast = 0.0

            while len(frames) < VERIFY_FRAME_COUNT:
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
                        logger.info("AUTHENTICATED!")
                        self._emit_verify("verify-match", True)
                        self._scanning = False
                        return
                    else:
                        logger.info("Username collision: %s is not %s",
                                     match_name, username)
                else:
                    logger.info("Wrong user! (%s)", match_name)

            time.sleep(0.05)

        logger.info("No match after %d attempts", attempt)
        if self._scanning:
            self._emit_verify("verify-retry-scan", False)
            logger.info("Ready for another verification touch.")
        else:
            logger.info("Verification loop stopped before retry emission.")

    # ------------------------------------------------------------------
    #  Callback emission (private)
    # ------------------------------------------------------------------

    def _emit_enroll(self, result, done):
        if self.on_enroll_status:
            self.on_enroll_status(result, done)

    def _emit_verify(self, result, done):
        if self.on_verify_status:
            self.on_verify_status(result, done)
