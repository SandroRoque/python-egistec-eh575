import json
import os
import math
import queue
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from egis_driver.streaming import FrameMessage, FrameStatus
from egis_matcher.frame import FrameSpec


class SequenceRecorder:
    """Bounded asynchronous writer for private, replayable frame sequences."""

    def __init__(self, directory, metadata=None, capacity=64):
        self.directory = Path(directory)
        self.metadata = dict(metadata or {})
        self.capacity = capacity
        self._queue = queue.Queue(maxsize=capacity)
        self._events = []
        self._dropped = 0
        self._incomplete = False
        self._thread = None
        self._frames = None

    def start(self):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.directory.chmod(0o700)
        frames_path = self.directory / "frames.bin"
        self._frames = open(frames_path, "wb")
        os.chmod(frames_path, 0o600)
        self._thread = threading.Thread(
            target=self._write_loop,
            name="egis-sequence-recorder",
            daemon=True,
        )
        self._thread.start()
        return self

    def observe(self, message):
        try:
            self._queue.put_nowait(message)
            return True
        except queue.Full:
            self._dropped += 1
            self._incomplete = True
            return False

    def _write_loop(self):
        while True:
            message = self._queue.get()
            if message is None:
                return
            offset = self._frames.tell()
            pixels = message.pixels or b""
            self._frames.write(pixels)
            event = asdict(message)
            event["frame_spec"] = asdict(message.frame_spec)
            event["status"] = message.status.value
            event["pixels"] = None
            event["offset"] = offset
            event["length"] = len(pixels)
            self._events.append(event)

    def close(self, complete=True):
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join()
        self._frames.flush()
        os.fsync(self._frames.fileno())
        self._frames.close()
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "complete": bool(complete and not self._incomplete),
            "recorder_dropped": self._dropped,
            "metadata": self.metadata,
            "events": self._events,
        }
        temporary = self.directory / "manifest.json.tmp"
        final = self.directory / "manifest.json"
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, final)
        self._thread = None

    def abort(self):
        self.close(complete=False)


def load_sequence(directory):
    directory = Path(directory)
    if ((directory / "manifest.json").stat().st_size > 8 * 1024 * 1024 or
            (directory / "frames.bin").stat().st_size > 64 * 1024 * 1024):
        raise ValueError("sequence exceeds replay size budget")
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema_version") != 1 or len(manifest.get("events", [])) > 4096:
        raise ValueError("unsupported sequence schema or event count")
    frame_data = (directory / "frames.bin").read_bytes()
    messages = []
    expected_offset = 0
    previous_sequence = 0
    previous_finished = -math.inf
    for stored_event in manifest["events"]:
        event = dict(stored_event)
        frame = event["frame_spec"]
        offset = int(event.pop("offset"))
        length = int(event.pop("length"))
        event["frame_spec"] = FrameSpec(**frame)
        event["status"] = FrameStatus(event["status"])
        spec = event["frame_spec"]
        if (spec.dtype != "uint8" or not 0 < spec.width <= 1024 or
                not 0 < spec.height <= 1024):
            raise ValueError("invalid sequence frame geometry")
        if (offset != expected_offset or length < 0 or offset + length > len(frame_data) or
                event["observed_bytes"] != length):
            raise ValueError("sequence frame offsets or lengths are corrupt")
        if event["status"] in {FrameStatus.VALID, FrameStatus.NO_CONTACT}:
            if length != spec.byte_count:
                raise ValueError("sequence contains a truncated frame")
        elif event["status"] is not FrameStatus.IO_ERROR and length != 0:
            raise ValueError("terminal sequence event contains pixels")
        if (event["sequence"] <= previous_sequence or event["dropped_before"] < 0 or
                not all(math.isfinite(event[key]) for key in (
                    "captured_started", "captured_finished", "contrast")) or
                event["captured_started"] < previous_finished or
                event["captured_finished"] < event["captured_started"]):
            raise ValueError("sequence order or timestamps are corrupt")
        previous_sequence = event["sequence"]
        previous_finished = event["captured_finished"]
        expected_offset = offset + length
        event["pixels"] = frame_data[offset:offset + length] or None
        messages.append(FrameMessage(**event))
    if expected_offset != len(frame_data):
        raise ValueError("sequence has unreferenced trailing frame bytes")
    return manifest, messages
