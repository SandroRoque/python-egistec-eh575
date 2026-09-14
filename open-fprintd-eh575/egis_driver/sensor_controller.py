"""One physical sensor owner with cancelable, epoch-bound client sessions."""

import threading
import time
from collections import deque
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass

from egis_driver.device_profile import EH575_PROFILE


class SensorCanceled(RuntimeError):
    """The caller no longer owns a valid sensor session."""


class SensorUnavailable(RuntimeError):
    """A sensor command could not complete within its resource budget."""


@dataclass
class _Command:
    method: str
    kwargs: dict
    session: object
    future: Future


class SensorController:
    """Serialize backend construction, reads, recovery, and release on one thread.

    Canceling a request never interrupts native USB code or starts another owner.
    Its late result is discarded; all later physical work waits for that call to
    return. Suspend invalidates sessions before enqueuing release, so stale
    capture producers cannot reconnect a sleeping device.
    """

    def __init__(self, backend=None, backend_factory=None, capacity=32,
                 command_timeout=10.0, startup_timeout=30.0):
        if capacity < 1 or command_timeout <= 0 or startup_timeout <= 0:
            raise ValueError("controller limits must be positive")
        if (backend is None) == (backend_factory is None):
            raise ValueError("provide either backend or backend_factory")
        self._backend = backend
        self._factory = backend_factory
        self._capacity = capacity
        self._command_timeout = command_timeout
        self._condition = threading.Condition()
        self._commands = deque()
        self._epoch = 0
        self._suspended = False
        self._closed = False
        self._release_future = None
        self._ready = Future()
        self._thread = threading.Thread(
            target=self._run, name="egis-sensor-owner", daemon=True)
        self._thread.start()
        try:
            self._ready.result(timeout=startup_timeout)
        except BaseException:
            self.close(timeout=0)
            raise

    def session(self, cancel_event=None):
        with self._condition:
            return SensorSession(self, self._epoch, cancel_event)

    def _valid(self, session):
        return (not self._closed and not self._suspended and
                session.epoch == self._epoch and
                not session.cancel_event.is_set())

    def _invalidate_pending(self):
        remaining = deque()
        for command in self._commands:
            if command.session is None:
                remaining.append(command)
            else:
                command.future.cancel()
        self._commands = remaining
        self._condition.notify_all()

    def suspend(self, timeout=2.0):
        with self._condition:
            if self._closed:
                return False
            if not self._suspended:
                self._suspended = True
                self._epoch += 1
                self._invalidate_pending()
                if self._release_future is None or self._release_future.done():
                    self._release_future = Future()
                    self._commands.append(_Command(
                        "release_for_sleep", {}, None, self._release_future))
                self._condition.notify_all()
            future = self._release_future
        try:
            future.result(timeout=timeout)
            return True
        except (TimeoutError, SensorUnavailable):
            # The release remains queued. A timeout must not allow a second
            # thread to perform physical cleanup concurrently with USB I/O.
            return False

    def resume(self):
        with self._condition:
            if self._closed:
                raise SensorCanceled("sensor controller is closed")
            if self._suspended:
                self._epoch += 1
                self._suspended = False
            # New commands are queued after the outstanding sleep release.
            self._condition.notify_all()

    def close(self, timeout=2.0):
        with self._condition:
            self._closed = True
            self._epoch += 1
            self._invalidate_pending()
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def _call(self, session, method, **kwargs):
        future = Future()
        with self._condition:
            if not self._valid(session):
                raise SensorCanceled("sensor session is inactive")
            # Remove abandoned requests before applying the queue budget.
            self._commands = deque(
                command for command in self._commands
                if not command.future.cancelled())
            if len(self._commands) >= self._capacity:
                raise SensorUnavailable("sensor command queue is full")
            self._commands.append(_Command(method, kwargs, session, future))
            self._condition.notify()
            deadline = time.monotonic() + self._command_timeout
            while True:
                if not self._valid(session):
                    future.cancel()
                    raise SensorCanceled("sensor session was canceled")
                if future.done():
                    return future.result()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    future.cancel()
                    raise SensorUnavailable("sensor command timed out")
                self._condition.wait(timeout=min(0.02, remaining))

    def _run(self):
        try:
            if self._backend is None:
                self._backend = self._factory()
            self.frame_spec = getattr(self._backend, "frame_spec", EH575_PROFILE.frame)
            self.touch_threshold = self._backend.touch_threshold
            self._ready.set_result(None)
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._commands or self._closed)
                    if not self._commands:
                        break
                    command = self._commands.popleft()
                    if command.future.cancelled():
                        continue
                    if command.session is not None and not self._valid(command.session):
                        command.future.cancel()
                        self._condition.notify_all()
                        continue
                try:
                    result = getattr(self._backend, command.method)(**command.kwargs)
                    error = None
                except Exception as caught:
                    result, error = None, caught
                with self._condition:
                    if not command.future.done():
                        if error is None:
                            command.future.set_result(result)
                        else:
                            command.future.set_exception(SensorUnavailable(
                                f"sensor command failed: {type(error).__name__}"))
                    self._condition.notify_all()
        except Exception as error:
            if not self._ready.done():
                self._ready.set_exception(error)
        finally:
            # Cleanup follows the same owner rule, including construction
            # timeouts and shutdown during a blocked read.
            if self._backend is not None:
                try:
                    self._backend.release_for_sleep()
                except Exception:
                    pass
            with self._condition:
                self._closed = True
                for command in self._commands:
                    command.future.cancel()
                self._commands.clear()
                self._condition.notify_all()


class SensorSession:
    """Backend-shaped acquisition interface tied to one lifecycle epoch."""

    def __init__(self, controller, epoch, cancel_event=None):
        self._controller = controller
        self.epoch = epoch
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.frame_spec = controller.frame_spec
        self.touch_threshold = controller.touch_threshold

    def with_cancel(self, cancel_event):
        return SensorSession(
            self._controller, self.epoch,
            _Cancellation(self.cancel_event, cancel_event))

    @property
    def active(self):
        with self._controller._condition:
            return self._controller._valid(self)

    def get_live_frame(self, read_timeout=1500):
        try:
            return self._controller._call(
                self, "get_live_frame", read_timeout=read_timeout)
        except SensorUnavailable:
            return None, 0.0

    def capture_presence_frame(self, read_timeout=1500):
        try:
            return self._controller._call(
                self, "capture_presence_frame", read_timeout=read_timeout)
        except SensorUnavailable:
            return None, 0.0, False

    def ensure_connected(self, force=False, reset=False):
        return self._availability("ensure_connected", force=force, reset=reset)

    def force_reconnect(self, reset=False):
        return self._availability("force_reconnect", reset=reset)

    def refresh_after_idle(self, idle_seconds=300):
        return self._availability("refresh_after_idle", idle_seconds=idle_seconds)

    def _availability(self, method, **kwargs):
        try:
            return self._controller._call(self, method, **kwargs)
        except SensorUnavailable:
            return False


class _Cancellation:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)
