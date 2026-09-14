import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from egis_driver.capture import CaptureCoordinator
from egis_driver.sensor_controller import SensorCanceled, SensorController
from egis_driver.streaming import CapturePump
from egis_matcher.frame import FrameSpec


class RecordingBackend:
    frame_spec = FrameSpec(4, 3)
    touch_threshold = 15.0

    def __init__(self, block=False):
        self.calls = [("construct", threading.get_ident())]
        self.started = threading.Event()
        self.unblock = threading.Event()
        self.block = block
        self.live_count = 0

    def _record(self, name):
        self.calls.append((name, threading.get_ident()))

    def get_live_frame(self, read_timeout=1500):
        self._record("read-start")
        self.live_count += 1
        self.started.set()
        if self.block:
            if not self.unblock.wait(5):
                raise TimeoutError("test backend was not unblocked")
        self._record("read-end")
        return bytes(12), 0.0

    def capture_presence_frame(self, read_timeout=1500):
        self._record("presence")
        return bytes(12), 0.0, False

    def ensure_connected(self, force=False, reset=False):
        self._record("ensure")
        return True

    def force_reconnect(self, reset=False):
        self._record("reconnect")
        return True

    def refresh_after_idle(self, idle_seconds=300):
        self._record("refresh")
        return True

    def release_for_sleep(self):
        self._record("release")


class SensorControllerTests(unittest.TestCase):
    def controller(self, backend=None, **kwargs):
        controller = SensorController(
            backend=backend, **kwargs) if backend is not None else SensorController(
                backend_factory=RecordingBackend, **kwargs)
        self.addCleanup(controller.close)
        return controller

    def wait_for_pending(self, controller, count):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with controller._condition:
                if len(controller._commands) >= count:
                    return
            time.sleep(0.001)
        self.fail("sensor requests were not queued")

    def test_construction_acquisition_recovery_and_release_share_one_owner(self):
        controller = self.controller()
        backend = controller._backend
        session = controller.session()
        session.ensure_connected()
        session.refresh_after_idle()
        coordinator = CaptureCoordinator(session, sleep=lambda _: None)
        coordinator.warm(2)
        coordinator.collect_touch(lambda: True, bytes(12))
        pump = CapturePump(session, session.frame_spec, 1, 1, release_frames=1).start()
        self.assertTrue(pump.wait(1))
        session.force_reconnect()
        self.assertTrue(controller.suspend())
        self.assertTrue(controller.close())
        owner_ids = {identity for _, identity in backend.calls}
        self.assertEqual(len(owner_ids), 1)
        self.assertNotIn(threading.get_ident(), owner_ids)
        self.assertIn("presence", [name for name, _ in backend.calls])
        self.assertIn("read-end", [name for name, _ in backend.calls])

    def test_cancellation_returns_before_blocked_io_and_discards_late_frame(self):
        backend = RecordingBackend(block=True)
        controller = self.controller(backend)
        canceled = threading.Event()
        stale = controller.session(canceled)
        with ThreadPoolExecutor(max_workers=2) as callers:
            try:
                old = callers.submit(stale.get_live_frame)
                self.assertTrue(backend.started.wait(1))
                canceled.set()
                with self.assertRaises(SensorCanceled):
                    old.result(timeout=1)
                new = callers.submit(controller.session().capture_presence_frame)
                self.wait_for_pending(controller, 1)
                self.assertNotIn("presence", [name for name, _ in backend.calls])
                backend.unblock.set()
                self.assertEqual(new.result(timeout=1), (bytes(12), 0.0, False))
                with self.assertRaises(SensorCanceled):
                    stale.force_reconnect()
            finally:
                backend.unblock.set()

    def test_suspend_invalidates_queued_work_and_release_precedes_resume_reads(self):
        backend = RecordingBackend(block=True)
        controller = self.controller(backend)
        old_session = controller.session()
        with ThreadPoolExecutor(max_workers=3) as callers:
            try:
                read = callers.submit(old_session.get_live_frame)
                self.assertTrue(backend.started.wait(1))
                reconnect = callers.submit(old_session.force_reconnect)
                self.wait_for_pending(controller, 1)
                self.assertFalse(controller.suspend(timeout=0))
                for future in (read, reconnect):
                    with self.assertRaises(SensorCanceled):
                        future.result(timeout=1)
                with self.assertRaises(SensorCanceled):
                    controller.session().get_live_frame()
                self.assertNotIn("release", [name for name, _ in backend.calls])
                controller.resume()
                resumed = callers.submit(controller.session().force_reconnect)
                backend.unblock.set()
                self.assertTrue(resumed.result(timeout=1))
                names = [name for name, _ in backend.calls]
                self.assertEqual(names, ["construct", "read-start", "read-end",
                                         "release", "reconnect"])
                with self.assertRaises(SensorCanceled):
                    old_session.get_live_frame()
            finally:
                backend.unblock.set()

    def test_stopping_pump_cancels_pending_read_without_another_usb_owner(self):
        backend = RecordingBackend(block=True)
        controller = self.controller(backend)
        pump = CapturePump(controller.session(), backend.frame_spec, 1, 1).start()
        try:
            self.assertTrue(backend.started.wait(1))
            pump.stop(timeout=1)
            self.assertFalse(pump.alive)
            self.assertEqual(pump.stream.drain(), [])
            self.assertEqual(backend.live_count, 1)
        finally:
            backend.unblock.set()

    def test_queue_budget_and_timeouts_do_not_execute_abandoned_work(self):
        backend = RecordingBackend(block=True)
        controller = self.controller(backend, capacity=1, command_timeout=0.2)
        with ThreadPoolExecutor(max_workers=2) as callers:
            try:
                read = callers.submit(controller.session().get_live_frame)
                self.assertTrue(backend.started.wait(1))
                queued = callers.submit(controller.session().capture_presence_frame)
                self.wait_for_pending(controller, 1)
                self.assertEqual(controller.session().capture_presence_frame(),
                                 (None, 0.0, False))
                self.assertEqual(read.result(timeout=1), (None, 0.0))
                self.assertEqual(queued.result(timeout=1), (None, 0.0, False))
                backend.unblock.set()
                self.assertTrue(controller.session().ensure_connected())
                self.assertNotIn("presence", [name for name, _ in backend.calls])
            finally:
                backend.unblock.set()

    def test_close_during_read_defers_cleanup_and_rejects_new_work(self):
        backend = RecordingBackend(block=True)
        controller = self.controller(backend)
        session = controller.session()
        with ThreadPoolExecutor(max_workers=1) as callers:
            try:
                read = callers.submit(session.get_live_frame)
                self.assertTrue(backend.started.wait(1))
                self.assertFalse(controller.close(timeout=0))
                with self.assertRaises(SensorCanceled):
                    read.result(timeout=1)
                with self.assertRaises(SensorCanceled):
                    controller.session().ensure_connected()
                backend.unblock.set()
                self.assertTrue(controller.close(timeout=1))
                self.assertEqual([name for name, _ in backend.calls],
                                 ["construct", "read-start", "read-end", "release"])
            finally:
                backend.unblock.set()

    def test_repeated_sleep_cycles_keep_only_one_pending_release(self):
        backend = RecordingBackend(block=True)
        controller = self.controller(backend, capacity=1)
        with ThreadPoolExecutor(max_workers=1) as callers:
            try:
                read = callers.submit(controller.session().get_live_frame)
                self.assertTrue(backend.started.wait(1))
                for _ in range(10):
                    self.assertFalse(controller.suspend(timeout=0))
                    controller.resume()
                with controller._condition:
                    self.assertEqual(len(controller._commands), 1)
                with self.assertRaises(SensorCanceled):
                    read.result(timeout=1)
                backend.unblock.set()
                self.assertTrue(controller.suspend(timeout=1))
                self.assertEqual([name for name, _ in backend.calls].count("release"), 1)
            finally:
                backend.unblock.set()

    def test_backend_exception_does_not_kill_owner_or_escape_acquisition(self):
        class FailingBackend(RecordingBackend):
            def get_live_frame(self, read_timeout=1500):
                raise OSError("synthetic USB error")

            def force_reconnect(self, reset=False):
                raise OSError("synthetic reconnect error")

        controller = self.controller(FailingBackend())
        session = controller.session()
        self.assertEqual(session.get_live_frame(), (None, 0.0))
        self.assertFalse(session.force_reconnect())
        self.assertEqual(session.capture_presence_frame(), (bytes(12), 0.0, False))


if __name__ == "__main__":
    unittest.main()
