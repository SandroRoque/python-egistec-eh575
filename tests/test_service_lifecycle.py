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
        self._capture_lock = threading.Lock()

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


class ControlledRecoveryService(EgisService):
    def __init__(self, *args, **kwargs):
        self.recovery_gate = threading.Event()
        super().__init__(*args, **kwargs)

    def prepare_sensor(self, reason, force=False):
        return True

    def _wait_for_finger_release(self, operation):
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
        return ControlledRecoveryService(
            driver=driver or FakeDriver(),
            matcher=FakeMatcher(),
            on_verify_status=lambda result, done: self.statuses.append((result, done)),
        )

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
        service = self._service()
        with mock.patch(
                "egis_driver.services.VERIFY_IDLE_LOG_INTERVAL_SECONDS",
                0.02):
            service.start_verify("testuser", "right-index-finger")
            self.assertTrue(self._wait_until(
                lambda: service._driver.capture_count >= 12,
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
        operation = ScanOperation(
            1,
            ("enroll", "testuser", "right-index-finger", True),
        )
        service._active_operation = operation

        with mock.patch("egis_driver.services.time.sleep"):
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

    def test_verification_requires_two_consecutive_matches(self):
        matcher = AlwaysMatchMatcher()
        statuses = []
        service = EgisService(
            driver=PresentDriver(),
            matcher=matcher,
            on_verify_status=lambda result, done: statuses.append((result, done)),
        )
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

        self.assertEqual(matcher.calls, 2)
        self.assertEqual(statuses, [("verify-match", True)])
        self.assertIsNone(service._active_operation)


if __name__ == "__main__":
    unittest.main()
