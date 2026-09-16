import threading
import time
import unittest
from unittest import mock

from egis_driver.services import EgisService, ScanOperation


class FakeDriver:
    touch_threshold = 31.0

    def __init__(self, block_first_capture=False):
        self.block_first_capture = block_first_capture
        self.first_capture_started = threading.Event()
        self.release_first_capture = threading.Event()
        self.release_count = 0
        self.capture_count = 0
        self.ensure_count = 0
        self._capture_lock = threading.Lock()

    def ensure_connected(self, force=False, reset=False):
        self.ensure_count += 1
        return True

    def refresh_after_idle(self, idle_seconds=300):
        return True

    def capture_presence_frame(self, read_timeout=1500):
        with self._capture_lock:
            self.capture_count += 1
            capture_number = self.capture_count
        if self.block_first_capture and capture_number == 1:
            self.first_capture_started.set()
            self.release_first_capture.wait(timeout=2.0)
        else:
            time.sleep(0.005)
        return bytes(103 * 52), 0.0, False

    def get_live_frame(self, read_timeout=1500):
        return bytes(103 * 52), 0.0

    def force_reconnect(self, reset=False):
        return True

    def release_for_sleep(self):
        self.release_count += 1


class FakeMatcher:
    last_verify_stats = {}

    def verify_finger_multiframe(self, frames, username=None, finger_name=None):
        return None, 0


class TemplateInvalidationTests(unittest.TestCase):
    def test_deletion_cancels_active_verification_and_invalidates_worker(self):
        matcher = mock.Mock()
        worker = mock.Mock()
        service = EgisService(driver=FakeDriver(), matcher=matcher,
                              matcher_worker=worker)
        self.addCleanup(service.close)
        operation = ScanOperation(1, ("verify", "test", None))
        service._active_operation = operation
        service.delete_enrolled_fingers("test")
        self.assertTrue(operation.cancel_event.is_set())
        self.assertIsNone(service._active_operation)
        matcher.delete_user_fingers.assert_called_once_with("test")
        worker.reload.assert_called_once_with()

    def test_deletion_invalidates_authoritative_and_touch_workers(self):
        matcher = mock.Mock()
        worker = mock.Mock()
        touch_worker = mock.Mock()
        touch_worker.reload.return_value = True
        service = EgisService(driver=FakeDriver(), matcher=matcher,
                              matcher_worker=worker)
        self.addCleanup(service.close)
        service._touch_matcher_worker = touch_worker

        service.delete_enrolled_fingers("test")

        worker.reload.assert_called_once_with()
        touch_worker.reload.assert_called_once_with()

    def test_failed_touch_reload_disables_only_shadow_matching(self):
        matcher = mock.Mock()
        worker = mock.Mock()
        touch_worker = mock.Mock()
        touch_worker.reload.return_value = False
        service = EgisService(driver=FakeDriver(), matcher=matcher,
                              matcher_worker=worker)
        self.addCleanup(service.close)
        service._touch_matcher_worker = touch_worker

        service.delete_enrolled_fingers("test")

        worker.reload.assert_called_once_with()
        touch_worker.close.assert_called_once_with()
        self.assertIsNone(service._touch_matcher_worker)


class EnrollmentMatcher(FakeMatcher):
    def __init__(self):
        self.enrollment = None

    def analyze_touch(self, frames):
        return {
            "frames": len(frames),
            "usable_frames": len(frames),
            "contrast_min": 30.0,
            "contrast_avg": 30.0,
            "contrast_max": 30.0,
            "quality_avg": 0.5,
            "max_keypoints": 10,
            "usable": True,
        }

    def enroll_finger(self, name, touch_groups):
        self.enrollment = (name, touch_groups)
        return True


class AlwaysMatchMatcher(FakeMatcher):
    def __init__(self):
        self.calls = 0
        self.last_verify_stats = {"best": {}, "calibrated": True}

    def verify_finger_multiframe(self, frames, username=None, finger_name=None):
        self.calls += 1
        return f"{username}_{finger_name}", 10


class PresentDriver(FakeDriver):
    def get_live_frame(self, read_timeout=1500):
        return bytes(103 * 52), 30.0


class PresentUntilReleasedDriver(FakeDriver):
    def __init__(self):
        super().__init__()
        self.released = threading.Event()

    def capture_presence_frame(self, read_timeout=1500):
        self.capture_count += 1
        present = not self.released.is_set()
        return bytes(103 * 52), 35.0 if present else 0.0, present


class CountingPresentDriver(PresentDriver):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def get_live_frame(self, read_timeout=1500):
        self.reads += 1
        return super().get_live_frame(read_timeout)


class ControlledRecoveryService(EgisService):
    def __init__(self, *args, **kwargs):
        self.recovery_gate = threading.Event()
        super().__init__(*args, **kwargs)

    def prepare_sensor(self, reason, force=False, sensor=None):
        return True

    def _wait_for_finger_release(self, operation, **kwargs):
        return

    def _start_resume_recovery(self):
        self._resume_ready.clear()

        def recover():
            self.recovery_gate.wait(timeout=2.0)
            self._resume_ready.set()

        threading.Thread(target=recover, daemon=True).start()


class ServiceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.statuses = []

    def _service(self, driver=None):
        service = ControlledRecoveryService(
            driver=driver or FakeDriver(),
            matcher=FakeMatcher(),
            on_verify_status=lambda result, done: self.statuses.append((result, done)),
        )
        self.addCleanup(service.close)
        return service

    def test_latency_summary_separates_capture_matching_and_shadow_costs(self):
        service = self._service()
        operation = ScanOperation(1, ("verify", "test", None))
        operation.created_at = 1.0
        operation.verify_latency = {
            "touch_started": 2.0,
            "first_frame_finished": 2.05,
            "capture_ms": 90.0,
            "queue_ms": 12.0,
            "match_ms": 210.0,
            "shadow_extraction_ms": 30.0,
            "shadow_comparison_ms": 45.0,
            "shadow_frames": 3,
            "attempts": 3,
            "accepted_attempt_ms": [100.0, 300.0, 480.0],
            "max_consecutive_accepts": 3,
            "inter_frame_ms": [30.0, 32.0],
            "deadline_expired": False,
        }
        with mock.patch("egis_driver.services.time.monotonic", return_value=2.5):
            summary = service._log_verify_latency(operation, "match")
        self.assertEqual(summary["request_to_touch_ms"], 1000.0)
        self.assertAlmostEqual(summary["touch_to_first_frame_ms"], 50.0)
        self.assertEqual(summary["touch_to_decision_ms"], 500.0)
        self.assertEqual(summary["matching_ms"], 210.0)
        self.assertEqual(summary["shadow_comparison_ms"], 45.0)
        self.assertEqual(summary["accepted_attempt_ms"], (100.0, 300.0, 480.0))

    def test_unhandled_verify_scan_failure_emits_one_terminal_error(self):
        statuses = []
        service = EgisService(
            driver=FakeDriver(),
            matcher=FakeMatcher(),
            on_verify_status=lambda result, done: statuses.append((result, done)),
        )
        self.addCleanup(service.close)

        def fail(operation, *args):
            raise RuntimeError("synthetic matcher failure")

        operation = service._start_scan(fail, ("verify", "testuser", None))
        operation.thread.join(timeout=1.0)

        self.assertFalse(operation.thread.is_alive())
        self.assertEqual(statuses, [("verify-unknown-error", True)])
        self.assertIsNone(service._active_operation)

    def test_unhandled_enroll_scan_failure_emits_terminal_failure(self):
        statuses = []
        service = EgisService(
            driver=FakeDriver(),
            matcher=FakeMatcher(),
            on_enroll_status=lambda result, done: statuses.append((result, done)),
        )
        self.addCleanup(service.close)

        def fail(operation, *args):
            raise RuntimeError("synthetic sensor failure")

        operation = service._start_scan(
            fail, ("enroll", "testuser", "right-index-finger", True))
        operation.thread.join(timeout=1.0)

        self.assertFalse(operation.thread.is_alive())
        self.assertEqual(statuses, [("enroll-failed", True)])
        self.assertIsNone(service._active_operation)

    def test_scan_failure_after_terminal_status_does_not_emit_duplicate(self):
        statuses = []
        service = EgisService(
            driver=FakeDriver(),
            matcher=FakeMatcher(),
            on_verify_status=lambda result, done: statuses.append((result, done)),
        )
        self.addCleanup(service.close)

        def finish_then_fail(operation, *args):
            service._emit_verify("verify-no-match", True, operation)
            raise RuntimeError("cleanup failure")

        operation = service._start_scan(
            finish_then_fail, ("verify", "testuser", None))
        operation.thread.join(timeout=1.0)

        self.assertEqual(statuses, [("verify-no-match", True)])
        self.assertIsNone(service._active_operation)

    def test_stale_scan_exception_cannot_emit_into_replacement(self):
        statuses = []
        stale_started = threading.Event()
        release_stale = threading.Event()
        replacement_ready = threading.Event()
        service = EgisService(
            driver=FakeDriver(),
            matcher=FakeMatcher(),
            on_verify_status=lambda result, done: statuses.append((result, done)),
        )
        self.addCleanup(service.close)

        def stale_target(operation, *args):
            stale_started.set()
            release_stale.wait(timeout=1.0)
            raise RuntimeError("stale worker failure")

        def replacement_target(operation, *args):
            replacement_ready.set()
            operation.cancel_event.wait(timeout=1.0)

        stale = service._start_scan(stale_target, ("verify", "alice", None))
        self.assertTrue(stale_started.wait(timeout=1.0))
        with mock.patch("egis_driver.services.SCAN_STOP_TIMEOUT_SECONDS", 0.01):
            replacement = service._start_scan(
                replacement_target, ("verify", "alice", "right-thumb"))
        self.assertTrue(replacement_ready.wait(timeout=1.0))
        release_stale.set()
        stale.thread.join(timeout=1.0)

        self.assertFalse(stale.thread.is_alive())
        self.assertIs(service._active_operation, replacement)
        self.assertEqual(statuses, [])

    def test_new_scan_can_start_after_unhandled_failure(self):
        statuses = []
        service = EgisService(
            driver=FakeDriver(),
            matcher=FakeMatcher(),
            on_verify_status=lambda result, done: statuses.append((result, done)),
        )
        self.addCleanup(service.close)

        def fail(operation, *args):
            raise RuntimeError("first scan failure")

        first = service._start_scan(fail, ("verify", "alice", None))
        first.thread.join(timeout=1.0)
        self.assertEqual(statuses, [("verify-unknown-error", True)])

        def succeed(operation, *args):
            service._emit_verify("verify-no-match", True, operation)

        second = service._start_scan(succeed, ("verify", "alice", None))
        second.thread.join(timeout=1.0)

        self.assertEqual(
            statuses,
            [("verify-unknown-error", True), ("verify-no-match", True)],
        )
        self.assertIsNone(service._active_operation)

    def _wait_until(self, predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_suspend_resume_restarts_same_verify_without_terminal_status(self):
        driver = FakeDriver()
        service = self._service(driver)
        service.recovery_gate.set()
        service.start_verify("testuser", "right-index-finger")
        original = service._active_operation

        service.suspend()
        self.assertEqual(self.statuses, [])
        self.assertEqual(driver.release_count, 1)

        service.resume()
        self.assertTrue(self._wait_until(
            lambda: (
                service._active_operation is not None and
                service._active_operation.generation > original.generation
            )
        ))
        self.assertEqual(service._active_operation.args, original.args)
        self.assertEqual(self.statuses, [])
        service.cancel()

    def test_new_verify_supersedes_suspended_verify_during_recovery(self):
        service = self._service()
        service.start_verify("testuser", "right-index-finger")
        suspended = service._active_operation
        service.suspend()
        service.resume()

        service.start_verify("testuser", "right-thumb")
        replacement = service._active_operation
        self.assertGreater(replacement.generation, suspended.generation)
        service.recovery_gate.set()
        time.sleep(0.05)

        self.assertIs(service._active_operation, replacement)
        self.assertEqual(replacement.args[2], "right-thumb")
        service.cancel()

    def test_blocked_stale_worker_cannot_revive_or_emit_into_replacement(self):
        driver = FakeDriver(block_first_capture=True)
        service = self._service(driver)
        service.start_verify("testuser", "right-index-finger")
        stale = service._active_operation
        self.assertTrue(driver.first_capture_started.wait(timeout=1.0))

        with mock.patch("egis_driver.services.SCAN_STOP_TIMEOUT_SECONDS", 0.02):
            service.start_verify("testuser", "right-thumb")
        replacement = service._active_operation
        driver.release_first_capture.set()
        self.assertTrue(self._wait_until(lambda: not stale.thread.is_alive()))

        self.assertIs(service._active_operation, replacement)
        self.assertTrue(stale.cancel_event.is_set())
        self.assertEqual(self.statuses, [])
        service.cancel()

    def test_idle_armed_verify_does_not_report_authentication_failure(self):
        driver = FakeDriver()
        service = self._service(driver)
        with mock.patch(
                "egis_driver.services.VERIFY_IDLE_LOG_INTERVAL_SECONDS",
                0.02):
            service.start_verify("testuser", "right-index-finger")
            self.assertTrue(self._wait_until(
                lambda: driver.capture_count >= 12,
            ))

        self.assertIsNotNone(service._active_operation)
        self.assertEqual(self.statuses, [])
        service.cancel()

    def test_enrollment_preserves_each_touch_as_a_separate_group(self):
        matcher = EnrollmentMatcher()
        service = ControlledRecoveryService(
            driver=FakeDriver(),
            matcher=matcher,
        )
        self.addCleanup(service.close)
        prompts = []
        service.on_enroll_prompt = prompts.append
        operation = ScanOperation(
            1,
            ("enroll", "testuser", "right-index-finger", True),
        )
        service._active_operation = operation

        with mock.patch.object(service, "_wait_for_finger_release") as wait_release:
            for value in range(10):
                frame = bytes([value]) * (103 * 52)
                service._handle_enroll(
                    operation,
                    frame,
                    "testuser",
                    "right-index-finger",
                )

        name, touch_groups = matcher.enrollment
        self.assertEqual(name, "testuser_right-index-finger")
        self.assertEqual(len(touch_groups), 10)
        self.assertEqual([len(group) for group in touch_groups], [1] * 10)
        self.assertEqual([group[0][0] for group in touch_groups], list(range(10)))
        self.assertEqual(wait_release.call_count, 9)
        wait_release.assert_called_with(
            operation, clear_frames=4, clear_seconds=0.3)
        self.assertEqual(
            prompts,
            ["lift-finger", "place-finger"] * 9,
        )

    def test_enrollment_does_not_advance_while_finger_remains_present(self):
        matcher = EnrollmentMatcher()
        driver = PresentUntilReleasedDriver()
        service = EgisService(driver=driver, matcher=matcher)
        self.addCleanup(service.close)
        operation = ScanOperation(
            1, ("enroll", "testuser", "right-index-finger", True))
        service._active_operation = operation
        worker = threading.Thread(
            target=service._handle_enroll,
            args=(operation, bytes(103 * 52), "testuser", "right-index-finger"),
        )
        worker.start()
        time.sleep(0.45)
        self.assertTrue(worker.is_alive())
        self.assertEqual(service._enroll_touch_count, 1)
        self.assertIsNone(matcher.enrollment)

        driver.released.set()
        worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())

    def test_third_consecutive_match_inside_budget_authenticates(self):
        matcher = AlwaysMatchMatcher()
        statuses = []
        service = EgisService(
            driver=PresentDriver(),
            matcher=matcher,
            on_verify_status=lambda result, done: statuses.append((result, done)),
        )
        self.addCleanup(service.close)
        operation = ScanOperation(
            1,
            ("verify", "testuser", "right-index-finger", True),
        )
        service._active_operation = operation

        with mock.patch("egis_driver.services.time.sleep"):
            service._handle_verify_continuous(
                operation,
                "testuser",
                "right-index-finger",
                bytes(103 * 52),
                30.0,
            )

        self.assertEqual(matcher.calls, 3)
        self.assertEqual(statuses, [("verify-match", True)])
        self.assertIsNone(service._active_operation)
        diagnostics = service.diagnostics_snapshot()
        self.assertEqual(diagnostics["capture"]["captured"], 3)
        self.assertEqual(diagnostics["matching"]["accept"], 3)

    def test_resuspend_cancels_recovery_and_preserves_paused_verification(self):
        driver = FakeDriver()
        service = EgisService(driver=driver, matcher=FakeMatcher())
        self.addCleanup(service.close)
        service.start_verify("testuser", "right-index-finger")
        original = service._active_operation
        service.suspend()
        self.assertIs(service._suspended_operation, original)
        service.resume()
        old_recovery = service._resume_recovery_thread
        service.suspend()
        old_recovery.join(timeout=1)
        self.assertFalse(old_recovery.is_alive())
        self.assertFalse(service._resume_ready.is_set())
        self.assertFalse(service._sensor_ready.is_set())
        self.assertIs(service._suspended_operation, original)
        self.assertIsNone(service._active_operation)
        with mock.patch("egis_driver.services.RESUME_USB_SETTLE_SECONDS", 0):
            service.resume()
            self.assertTrue(self._wait_until(
                lambda: service._active_operation is not None))
        self.assertEqual(service._active_operation.args, original.args)

    def test_stale_warmup_cannot_restore_readiness_after_suspend(self):
        service = EgisService(driver=FakeDriver(), matcher=FakeMatcher())
        self.addCleanup(service.close)
        session = service._sensor.session()
        service.suspend()
        from egis_driver.sensor_controller import SensorCanceled
        with self.assertRaises(SensorCanceled):
            service._set_sensor_readiness(session, True)
        self.assertFalse(service._sensor_ready.is_set())

    def test_suspend_releases_sensor_while_matcher_remains_blocked(self):
        entered = threading.Event()
        unblock = threading.Event()

        class SlowMatcher(AlwaysMatchMatcher):
            def verify_finger_multiframe(self, *args, **kwargs):
                entered.set()
                if not unblock.wait(5):
                    raise TimeoutError("test matcher was not unblocked")
                return super().verify_finger_multiframe(*args, **kwargs)

        driver = CountingPresentDriver()
        statuses = []
        service = EgisService(
            driver=driver, matcher=SlowMatcher(),
            on_verify_status=lambda *status: statuses.append(status))
        self.addCleanup(service.close)

        def verify(operation, mode, username, finger):
            service._handle_verify_continuous(operation, username, finger)

        operation = service._start_scan(
            verify, ("verify", "testuser", "right-index-finger"))
        try:
            self.assertTrue(entered.wait(1))
            reads_when_matching_started = driver.reads
            self.assertTrue(self._wait_until(
                lambda: driver.reads >= reads_when_matching_started + 5))
            with mock.patch("egis_driver.services.SCAN_STOP_TIMEOUT_SECONDS", 0.03):
                service.suspend()
            self.assertEqual(driver.release_count, 1)
            stopped_at = driver.reads
            time.sleep(0.04)
            self.assertEqual(driver.reads, stopped_at)
            self.assertIs(service._suspended_operation, operation)
            unblock.set()
            operation.thread.join(timeout=1)
            self.assertFalse(operation.thread.is_alive())
            self.assertEqual(statuses, [])
        finally:
            unblock.set()


if __name__ == "__main__":
    unittest.main()
