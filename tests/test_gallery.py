import hashlib
from pathlib import Path
import tempfile
import unittest

import numpy as np

from egis_driver.gallery_storage import load_gallery, save_gallery
from egis_matcher.feature_engine import FeatureRecord, FingerprintImage
from egis_matcher.frame import FrameSpec
from egis_matcher.gallery import GalleryBuilder, StreamingGalleryMatcher


SPEC = FrameSpec(103, 52)


class FakeEngine:
    VERSION = "test-1"
    available = True

    def config(self):
        return {"engine": "fake", "version": self.VERSION}

    def extract_record(self, image):
        assert isinstance(image, FingerprintImage)
        digest = hashlib.sha256(image.image.tobytes()).digest()
        return FeatureRecord("fake", self.VERSION, digest), "ok", 1.0

    def compare_records(self, probe, enrolled):
        return float(sum(a == b for a, b in zip(probe.data, enrolled.data)))


def sweep(seed, count=12):
    scene = np.random.default_rng(seed).integers(
        0, 256, size=(52, 103 + count * 3), dtype=np.uint8)
    return [scene[:, index * 3:index * 3 + 103].tobytes()
            for index in range(count)]


class GalleryTests(unittest.TestCase):
    def test_builder_balances_presentations_and_keeps_components_separate(self):
        gallery = GalleryBuilder(FakeEngine(), SPEC).build(
            "alice_right-index-finger", [sweep(1), sweep(2)])

        self.assertEqual(gallery.presentations, 2)
        self.assertLessEqual(len(gallery.entries), 48)
        self.assertEqual({entry.presentation for entry in gallery.entries}, {0, 1})
        self.assertIn("frame", {entry.representation for entry in gallery.entries})
        self.assertIn("component", {entry.representation for entry in gallery.entries})

    def test_storage_round_trip_contains_only_opaque_records(self):
        gallery = GalleryBuilder(FakeEngine(), SPEC).build("alice", [sweep(3)])
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "gallery"
            manifest = save_gallery(gallery, destination)
            restored, loaded = load_gallery(destination)

            self.assertFalse(manifest["contains_raw_frames"])
            self.assertEqual(restored.identity, gallery.identity)
            self.assertEqual(
                [entry.record.data for entry in restored.entries],
                [entry.record.data for entry in gallery.entries],
            )
            self.assertNotIn("image", loaded)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (destination / "records.bin").stat().st_mode & 0o777, 0o600)

    def test_storage_rejects_tampering(self):
        gallery = GalleryBuilder(FakeEngine(), SPEC).build("alice", [sweep(4)])
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "gallery"
            save_gallery(gallery, destination)
            with (destination / "records.bin").open("ab") as stream:
                stream.write(b"x")
            with self.assertRaises(ValueError):
                load_gallery(destination)

    def test_stream_scores_but_never_authenticates_uncalibrated_gallery(self):
        engine = FakeEngine()
        right = GalleryBuilder(engine, SPEC).build("right", [[sweep(5)[0]]])
        pinky = GalleryBuilder(engine, SPEC).build("pinky", [[sweep(6)[0]]])
        matcher = StreamingGalleryMatcher(
            engine, {"right": right, "pinky": pinky}, SPEC)
        matcher.begin_touch()

        decision = matcher.observe(sweep(5)[0], 1)

        self.assertFalse(decision.accepted)
        self.assertIsNone(decision.identity)
        self.assertEqual(decision.reason, "shadow_uncalibrated")
        self.assertEqual(decision.metrics["best_identity"], "right")
        matcher.discontinuity()
        self.assertEqual(matcher.frames_seen, 0)


if __name__ == "__main__":
    unittest.main()
