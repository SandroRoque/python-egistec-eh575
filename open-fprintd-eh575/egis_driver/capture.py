import time
import threading
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum


class CaptureOutcome(str, Enum):
    CAPTURED = "captured"
    INCOMPLETE = "incomplete"
    NO_TOUCH = "no_touch"
    IO_ERROR = "io_error"
    DEVICE_UNAVAILABLE = "device_unavailable"
    CANCELED = "canceled"


@dataclass(frozen=True)
class CaptureResult:
    outcome: CaptureOutcome
    frames: tuple[bytes, ...] = ()
    elapsed_ms: float = 0.0
    read_failures: int = 0
    details: dict = field(default_factory=dict)


class CaptureCoordinator:
    """Hardware-facing acquisition, independent of session and matching policy."""

    def __init__(self, backend, sleep=time.sleep, monotonic=time.monotonic):
        self.backend = backend
        self._sleep = sleep
        self._monotonic = monotonic
        self._counts = Counter()
        self._counts_lock = threading.Lock()

    def _record(self, result):
        with self._counts_lock:
            self._counts[result.outcome.value] += 1
            self._counts["read_failures"] += result.read_failures
        return result

    def metrics_snapshot(self):
        with self._counts_lock:
            return dict(self._counts)

    def warm(self, frame_count=4, read_timeout=250, backend=None):
        backend = backend if backend is not None else self.backend
        started = self._monotonic()
        contrasts = []
        failures = 0
        for _ in range(frame_count):
            frame, contrast, _ = backend.capture_presence_frame(
                read_timeout=read_timeout)
            if frame is None:
                failures += 1
            else:
                contrasts.append(float(contrast))
            self._sleep(0.03)
        return self._record(CaptureResult(
            CaptureOutcome.CAPTURED if contrasts else CaptureOutcome.DEVICE_UNAVAILABLE,
            elapsed_ms=(self._monotonic() - started) * 1000.0,
            read_failures=failures,
            details={"valid_frames": len(contrasts), "contrasts": tuple(contrasts)},
        ))

    def is_verify_touch(self, contrast, baseline_contrast,
                        presence_threshold=24.0, presence_delta=8.0):
        if contrast >= self.backend.touch_threshold:
            return True, "driver-threshold"
        if contrast >= presence_threshold:
            return True, "verify-threshold"
        if baseline_contrast is not None and contrast >= baseline_contrast + presence_delta:
            return True, "baseline-delta"
        return False, "none"

    def collect_window(self, frame_count, is_active, initial_frame=None,
                       initial_contrast=0.0, min_contrast=15.0):
        started = self._monotonic()
        frames = []
        failures = 0
        if initial_frame is not None and initial_contrast >= min_contrast:
            frames.append(initial_frame)
        while len(frames) < frame_count and is_active():
            frame, contrast = self.backend.get_live_frame()
            if frame is None:
                failures += 1
                break
            if contrast < min_contrast:
                break
            frames.append(frame)
            self._sleep(0.01)
        if not is_active():
            outcome = CaptureOutcome.CANCELED
        elif len(frames) == frame_count:
            outcome = CaptureOutcome.CAPTURED
        elif failures:
            outcome = CaptureOutcome.IO_ERROR
        else:
            outcome = CaptureOutcome.INCOMPLETE
        return self._record(CaptureResult(
            outcome, tuple(frames),
            (self._monotonic() - started) * 1000.0, failures))

    def collect_touch(self, is_active, initial_frame, max_duration=3.0,
                      min_contrast=15.0, backend=None):
        backend = backend if backend is not None else self.backend
        started = self._monotonic()
        frames = [initial_frame]
        failures = 0
        while self._monotonic() - started < max_duration and is_active():
            frame, contrast = backend.get_live_frame()
            if frame is None:
                failures += 1
                break
            if contrast < min_contrast:
                break
            frames.append(frame)
        if not is_active():
            outcome = CaptureOutcome.CANCELED
        elif failures:
            outcome = CaptureOutcome.IO_ERROR
        else:
            outcome = CaptureOutcome.CAPTURED
        return self._record(CaptureResult(
            outcome, tuple(frames),
            (self._monotonic() - started) * 1000.0, failures))
