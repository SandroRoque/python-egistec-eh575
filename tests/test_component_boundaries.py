import subprocess
import sys
import unittest

from egis_driver.capture import CaptureCoordinator, CaptureOutcome
from egis_driver.observability import summarize_metric_lines
from egis_matcher import (
    ConfirmationPolicy,
    ConfirmationTracker,
    FrameSpec,
    MatcherCore,
    MatcherConfig,
)
from egis_matcher.decision import MatchOutcome


class ScriptedBackend:
    touch_threshold = 31.0

    def __init__(self, frames=(), presence=()):
        self.frames = list(frames)
        self.presence = list(presence)

    def get_live_frame(self, read_timeout=1500):
        return self.frames.pop(0) if self.frames else (None, 0.0)

    def capture_presence_frame(self, read_timeout=1500):
        return self.presence.pop(0) if self.presence else (None, 0.0, False)


class MatcherLibraryBoundaryTests(unittest.TestCase):
    def test_live_imports_do_not_load_experimental_engines(self):
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; import egis_driver.services; "
             "assert all(name not in sys.modules for name in "
             "('egis_matcher.sourceafis', 'egis_matcher.gallery', 'egis_matcher.atlas'))"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_package_import_does_not_load_linux_or_usb_modules(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from egis_matcher import MatcherCore, FeatureAtlas, StreamingAtlasMatcher; "
                "assert all(name not in sys.modules for name in ('usb', 'dbus', 'gi'))",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_core_rejects_malformed_frame_as_unscorable(self):
        core = MatcherCore(FrameSpec(103, 52), MatcherConfig())

        decision = core.evaluate(
            [b"short"],
            username="alice",
            finger_name="right-index-finger",
            train_descriptors=None,
            descriptor_lookup={},
            cached_templates={},
            flann=None,
            thresholds={},
            calibrated=True,
            legacy_templates=[],
        )

        self.assertEqual(decision.outcome, MatchOutcome.UNSCORABLE)
        self.assertEqual(decision.reason, "invalid_frame")

    def test_confirmation_requires_consecutive_accepts(self):
        tracker = ConfirmationTracker(ConfirmationPolicy(3, 2))

        self.assertFalse(tracker.record(True))
        self.assertFalse(tracker.record(False))
        self.assertFalse(tracker.record(True))
        self.assertTrue(tracker.record(True))

    def test_confirmation_requires_same_identity(self):
        tracker = ConfirmationTracker(ConfirmationPolicy(3, 2))

        self.assertFalse(tracker.record(True, "roque_right-thumb"))
        self.assertFalse(tracker.record(True, "roque_right-index-finger"))
        self.assertEqual(tracker.last_reset_reason, "identity_switch")
        self.assertTrue(tracker.record(True, "roque_right-index-finger"))

    def test_confirmation_reset_clears_identity(self):
        tracker = ConfirmationTracker(ConfirmationPolicy(3, 2))

        tracker.record(True, "roque_right-thumb")
        tracker.reset("contact_end")

        self.assertIsNone(tracker.identity)
        self.assertEqual(tracker.last_reset_reason, "contact_end")


class CaptureCoordinatorTests(unittest.TestCase):
    def test_complete_window_is_separate_from_matching(self):
        frame = bytes(103 * 52)
        coordinator = CaptureCoordinator(
            ScriptedBackend(frames=[(frame, 30.0), (frame, 30.0)]),
            sleep=lambda _: None,
        )

        result = coordinator.collect_window(
            3,
            lambda: True,
            initial_frame=frame,
            initial_contrast=30.0,
        )

        self.assertEqual(result.outcome, CaptureOutcome.CAPTURED)
        self.assertEqual(len(result.frames), 3)
        self.assertEqual(coordinator.metrics_snapshot()["captured"], 1)

    def test_read_failure_is_not_a_match_rejection(self):
        frame = bytes(103 * 52)
        coordinator = CaptureCoordinator(
            ScriptedBackend(frames=[(None, 0.0)]),
            sleep=lambda _: None,
        )

        result = coordinator.collect_window(
            3,
            lambda: True,
            initial_frame=frame,
            initial_contrast=30.0,
        )

        self.assertEqual(result.outcome, CaptureOutcome.IO_ERROR)
        self.assertEqual(result.read_failures, 1)

    def test_warmup_distinguishes_unavailable_device(self):
        coordinator = CaptureCoordinator(
            ScriptedBackend(),
            sleep=lambda _: None,
        )

        result = coordinator.warm(frame_count=2)

        self.assertEqual(result.outcome, CaptureOutcome.DEVICE_UNAVAILABLE)
        self.assertEqual(result.read_failures, 2)


class ObservabilityTests(unittest.TestCase):
    def test_summary_keeps_failure_domains_separate(self):
        summary = summarize_metric_lines([
            "[METRIC] component=verification_capture outcome=io_error frames=1",
            "[METRIC] component=verification_capture outcome=captured frames=3",
            "[METRIC] component=matching outcome=reject reason=image_mismatch",
        ])

        self.assertEqual(summary["components"]["verification_capture"], {
            "captured": 1,
            "io_error": 1,
        })
        self.assertEqual(summary["components"]["matching"], {"reject": 1})


if __name__ == "__main__":
    unittest.main()
