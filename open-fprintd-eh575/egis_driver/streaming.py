import multiprocessing
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum

from egis_matcher.decision import MatchDecision, MatchOutcome
from egis_matcher.frame import FrameSpec
from egis_matcher.matcher_config import MatcherConfig


class FrameStatus(str, Enum):
    VALID = "valid"
    NO_CONTACT = "no_contact"
    IO_ERROR = "io_error"
    CONTACT_END = "contact_end"


@dataclass(frozen=True)
class FrameMessage:
    sequence: int
    generation: int
    capture_epoch: int
    captured_started: float
    captured_finished: float
    frame_spec: FrameSpec
    status: FrameStatus
    pixels: bytes | None = None
    contrast: float = 0.0
    observed_bytes: int = 0
    dropped_before: int = 0


class FrameStream:
    """Bounded newest-frame queue with explicit discontinuity accounting."""

    def __init__(self, capacity=16):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._dropped = 0

    def publish(self, message):
        with self._lock:
            while self._queue.full():
                try:
                    removed = self._queue.get_nowait()
                    self._dropped += 1 + removed.dropped_before
                except queue.Empty:
                    break
            if self._dropped:
                message = FrameMessage(
                    **{
                        **message.__dict__,
                        "dropped_before": message.dropped_before + self._dropped,
                    }
                )
                self._dropped = 0
            self._queue.put_nowait(message)

    def receive(self, timeout=None):
        return self._queue.get(timeout=timeout)

    def drain(self):
        messages = []
        while True:
            try:
                messages.append(self._queue.get_nowait())
            except queue.Empty:
                return messages


class CapturePump:
    """Continuously owns sensor reads for one detected contact."""

    def __init__(self, backend, frame_spec, generation, capture_epoch,
                 initial_frame=None, initial_contrast=0.0, capacity=16,
                 min_contrast=15.0, release_frames=10,
                 monotonic=time.monotonic, sleep=time.sleep,
                 on_message=None):
        self.backend = backend
        self.frame_spec = frame_spec
        self.generation = generation
        self.capture_epoch = capture_epoch
        self.initial_frame = initial_frame
        self.initial_contrast = initial_contrast
        self.min_contrast = min_contrast
        self.release_frames = release_frames
        self.stream = FrameStream(capacity)
        self._monotonic = monotonic
        self._sleep = sleep
        self._on_message = on_message
        self._stop = threading.Event()
        self._thread = None
        self._sequence = 0

    def start(self):
        if self._thread is not None:
            raise RuntimeError("capture pump already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"egis-capture-{self.generation}-{self.capture_epoch}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return not self.alive

    @property
    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    def _publish(self, status, started, finished, pixels=None, contrast=0.0):
        self._sequence += 1
        message = FrameMessage(
            sequence=self._sequence,
            generation=self.generation,
            capture_epoch=self.capture_epoch,
            captured_started=started,
            captured_finished=finished,
            frame_spec=self.frame_spec,
            status=status,
            pixels=pixels,
            contrast=float(contrast),
            observed_bytes=len(pixels) if pixels is not None else 0,
        )
        self.stream.publish(message)
        if self._on_message is not None:
            self._on_message(message)

    def _run(self):
        low_contrast = 0
        pending = self.initial_frame
        pending_contrast = self.initial_contrast
        while not self._stop.is_set():
            started = self._monotonic()
            if pending is not None:
                frame, contrast = pending, pending_contrast
                pending = None
            else:
                frame, contrast = self.backend.get_live_frame()
            finished = self._monotonic()
            if frame is None:
                self._publish(FrameStatus.IO_ERROR, started, finished)
                low_contrast += 1
            elif len(frame) != self.frame_spec.byte_count:
                self._publish(
                    FrameStatus.IO_ERROR, started, finished, frame, contrast)
                low_contrast += 1
            elif contrast < self.min_contrast:
                low_contrast += 1
                self._publish(
                    FrameStatus.NO_CONTACT, started, finished,
                    bytes(frame), contrast)
            else:
                low_contrast = 0
                self._publish(
                    FrameStatus.VALID, started, finished, bytes(frame), contrast)
            self._sleep(0.001)
            if low_contrast >= self.release_frames:
                now = self._monotonic()
                self._publish(FrameStatus.CONTACT_END, now, now)
                return


def _matcher_process(connection, persistence_root, frame_spec, config):
    from egis_driver.fingerprint_matcher import FingerprintMatcher
    from egis_driver.persistence import Persistence

    def build():
        return FingerprintMatcher(
            persistence=Persistence(persistence_root),
            matcher_config=MatcherConfig.from_dict(config),
            frame_spec=frame_spec,
        )

    try:
        matcher = build()
        connection.send(("ready",))
    except Exception as error:
        connection.send(("startup_error", type(error).__name__))
        return
    while True:
        message = connection.recv()
        command = message[0]
        if command == "shutdown":
            return
        if command == "reload":
            matcher = build()
            connection.send(("reloaded",))
            continue
        if command == "evaluate":
            _, request_id, generation, frames, username, finger_name = message
            try:
                decision = matcher.evaluate_multiframe(
                    frames, username=username, finger_name=finger_name)
            except Exception as error:
                decision = MatchDecision(
                    MatchOutcome.UNSCORABLE,
                    reason="matcher_exception",
                    metrics={"error_type": type(error).__name__},
                )
            connection.send(("decision", request_id, generation, decision))


class MatcherWorker:
    """Persistent spawned matcher process with timeout and restart handling."""

    def __init__(self, persistence_root, frame_spec, config=None, timeout=2.0,
                 startup_timeout=10.0):
        self.persistence_root = str(persistence_root)
        self.frame_spec = frame_spec
        self.config = (config or MatcherConfig()).to_dict()
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self._context = multiprocessing.get_context("spawn")
        self._process = None
        self._connection = None
        self._request_id = 0
        self._lock = threading.Lock()

    def _start(self):
        parent, child = self._context.Pipe()
        process = self._context.Process(
            target=_matcher_process,
            args=(child, self.persistence_root, self.frame_spec, self.config),
            name="egis-matcher",
            daemon=True,
        )
        process.start()
        child.close()
        self._connection = parent
        self._process = process
        if not parent.poll(self.startup_timeout):
            self._stop()
            raise RuntimeError("matcher worker startup timed out")
        response = parent.recv()
        if response != ("ready",):
            self._stop()
            detail = response[1] if len(response) > 1 else "unknown"
            raise RuntimeError(f"matcher worker startup failed: {detail}")

    def _stop(self):
        connection, process = self._connection, self._process
        self._connection = None
        self._process = None
        if connection is not None:
            try:
                connection.send(("shutdown",))
            except (BrokenPipeError, EOFError, OSError):
                pass
            connection.close()
        if process is not None:
            process.join(timeout=0.25)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1.0)

    def close(self):
        with self._lock:
            self._stop()

    def restart(self):
        self._stop()
        self._start()

    def evaluate(self, frames, username, finger_name, generation):
        with self._lock:
            if self._process is None or not self._process.is_alive():
                self.restart()
            self._request_id += 1
            request_id = self._request_id
            try:
                self._connection.send((
                    "evaluate", request_id, generation, tuple(frames),
                    username, finger_name,
                ))
                if not self._connection.poll(self.timeout):
                    self.restart()
                    return MatchDecision(
                        MatchOutcome.UNSCORABLE,
                        reason="matcher_timeout",
                        metrics={},
                    )
                kind, response_id, response_generation, decision = (
                    self._connection.recv())
                if kind != "decision" or response_id != request_id:
                    raise RuntimeError("matcher protocol mismatch")
                if response_generation != generation:
                    return MatchDecision(
                        MatchOutcome.UNSCORABLE,
                        reason="stale_generation",
                        metrics={},
                    )
                return decision
            except (BrokenPipeError, EOFError, OSError):
                self.restart()
                return MatchDecision(
                    MatchOutcome.UNSCORABLE,
                    reason="matcher_worker_failed",
                    metrics={},
                )

    def reload(self):
        with self._lock:
            self.restart()


class DirectMatcherWorker:
    """Test/offline adapter implementing the worker contract in-process."""

    def __init__(self, matcher):
        self.matcher = matcher

    def evaluate(self, frames, username, finger_name, generation):
        if hasattr(self.matcher, "evaluate_multiframe"):
            return self.matcher.evaluate_multiframe(
                frames, username=username, finger_name=finger_name)
        identity, score = self.matcher.verify_finger_multiframe(
            frames, username=username, finger_name=finger_name)
        metrics = dict(getattr(self.matcher, "last_verify_stats", {}))
        return MatchDecision(
            MatchOutcome.ACCEPT if identity else MatchOutcome.REJECT,
            identity,
            score,
            metrics.get("reject_reason"),
            metrics,
        )

    def reload(self):
        return None

    def close(self):
        return None
