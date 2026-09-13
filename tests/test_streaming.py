import tempfile
import unittest
from pathlib import Path

from egis_driver.sequence_recording import SequenceRecorder, load_sequence
from egis_driver.streaming import (
    CapturePump,
    FrameMessage,
    FrameStatus,
    FrameStream,
    MatcherWorker,
)
from egis_matcher.frame import FrameSpec


SPEC = FrameSpec(4, 3)


_DEFAULT_PIXELS = object()


def message(sequence, status=FrameStatus.VALID, pixels=_DEFAULT_PIXELS):
    if pixels is _DEFAULT_PIXELS:
        pixels = bytes([sequence] * SPEC.byte_count)
    return FrameMessage(
        sequence=sequence,
        generation=7,
        capture_epoch=2,
        captured_started=float(sequence),
        captured_finished=float(sequence) + 0.01,
        frame_spec=SPEC,
        status=status,
        pixels=pixels,
        contrast=20.0,
        observed_bytes=len(pixels) if pixels is not None else 0,
    )


class FakeBackend:
    def __init__(self, frames):
        self.frames = iter(frames)

    def get_live_frame(self):
        try:
            return next(self.frames)
        except StopIteration:
            return bytes(SPEC.byte_count), 0.0


class StreamingTests(unittest.TestCase):
    def test_bounded_stream_drops_oldest_and_reports_discontinuity(self):
        stream = FrameStream(capacity=2)
        stream.publish(message(1))
        stream.publish(message(2))
        stream.publish(message(3))
        stream.publish(message(4))
        self.assertEqual(stream.receive().sequence, 3)
        newest = stream.receive()
        self.assertEqual(newest.sequence, 4)
        self.assertEqual(newest.dropped_before, 1)

    def test_capture_pump_preserves_order_status_and_raw_observations(self):
        observed = []
        backend = FakeBackend([
            (bytes(range(SPEC.byte_count)), 20.0),
            (bytes(SPEC.byte_count), 2.0),
            (bytes(SPEC.byte_count), 1.0),
        ])
        pump = CapturePump(
            backend, SPEC, 7, 2, release_frames=2,
            on_message=observed.append,
        ).start()
        self.assertTrue(pump.wait(1.0))
        self.assertEqual(
            [item.status for item in observed],
            [FrameStatus.VALID, FrameStatus.NO_CONTACT,
             FrameStatus.NO_CONTACT, FrameStatus.CONTACT_END],
        )
        self.assertEqual([item.sequence for item in observed], [1, 2, 3, 4])
        self.assertIsNotNone(observed[1].pixels)

    def test_private_recorder_round_trips_ordered_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "touch"
            recorder = SequenceRecorder(destination, {"label": "test"}).start()
            recorder.observe(message(1))
            recorder.observe(message(2, FrameStatus.CONTACT_END, pixels=None))
            recorder.close()
            manifest, events = load_sequence(destination)
            self.assertTrue(manifest["complete"])
            self.assertEqual([event.sequence for event in events], [1, 2])
            self.assertEqual(events[0].pixels, bytes([1] * SPEC.byte_count))
            self.assertIsNone(events[1].pixels)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (destination / "frames.bin").stat().st_mode & 0o777, 0o600)

    def test_matcher_worker_round_trip_without_templates(self):
        with tempfile.TemporaryDirectory() as temporary:
            worker = MatcherWorker(temporary, SPEC, timeout=5.0)
            try:
                decision = worker.evaluate(
                    [bytes(SPEC.byte_count)] * 3,
                    "nobody", None, generation=4,
                )
                self.assertIn(decision.reason, ("no_templates", "no_candidates"))
            finally:
                worker.close()


if __name__ == "__main__":
    unittest.main()
