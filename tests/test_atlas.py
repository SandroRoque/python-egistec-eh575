import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest import mock

import numpy as np

from egis_driver.atlas_storage import load_atlas, save_atlas
from egis_driver.sequence_evaluation import enroll_sequences, evaluate_sequence
from egis_driver.sequence_recording import SequenceRecorder, load_sequence
from egis_driver.streaming import FrameMessage, FrameStatus
from egis_matcher.atlas import FeatureAtlas, StreamingAtlasMatcher
from egis_matcher.frame import FrameSpec
from egis_matcher.sequence import TouchTracker


SPEC = FrameSpec(103, 52)


def synthetic_sweep(seed=42):
    scene = np.random.default_rng(seed).integers(0, 256, (60, 160), dtype=np.uint8)
    return [scene[:52, x:x + 103].copy().tobytes() for x in (0, 16, 32)]


def build_atlas(frames):
    tracker = TouchTracker(SPEC)
    for index, frame in enumerate(frames):
        tracker.observe(frame, index + 1)
    atlas = FeatureAtlas(SPEC)
    atlas.add_touch(tracker.observations)
    return atlas


def record(directory, frames, role="enrollment", finger="right-index-finger"):
    recorder = SequenceRecorder(
        directory, {"role": role, "finger": finger}).start()
    try:
        for index, pixels in enumerate([*frames, None], 1):
            recorder.observe(FrameMessage(
                index, 1, 1, float(index), float(index) + 0.1, SPEC,
                FrameStatus.VALID if pixels is not None else FrameStatus.CONTACT_END,
                pixels=pixels, contrast=30.0 if pixels else 0.0,
                observed_bytes=len(pixels) if pixels else 0))
    finally:
        recorder.close()


class AtlasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frames = synthetic_sweep()

    def test_duplicate_enrollment_frames_do_not_inflate_coverage(self):
        atlas = build_atlas([self.frames[0]] * 3)
        self.assertEqual(len(atlas.keyframes), 1)
        self.assertEqual(atlas.redundant_frames, 2)
        self.assertEqual(atlas.keyframes[0].observation.raw_frame, self.frames[0])

    def test_repeated_live_patch_does_not_confirm_but_new_region_does(self):
        matcher = StreamingAtlasMatcher(build_atlas(self.frames))
        first = matcher.observe(self.frames[0])
        repeat = matcher.observe(self.frames[0])
        moved = matcher.observe(self.frames[1])
        self.assertFalse(first["evidence_sufficient"])
        self.assertFalse(repeat["evidence_sufficient"])
        self.assertEqual(repeat["admitted_frames"], 1)
        self.assertEqual(repeat["reason"], "redundant_region")
        self.assertGreater(moved["supported_cells"], first["supported_cells"])
        self.assertTrue(moved["evidence_sufficient"])
        self.assertFalse(moved["calibrated"])

    def test_gap_resets_support_and_cannot_confirm_with_pre_gap_frame(self):
        matcher = StreamingAtlasMatcher(build_atlas(self.frames))
        matcher.observe(self.frames[0], 1)
        matcher.discontinuity()
        result = matcher.observe(self.frames[1], 3)
        self.assertFalse(result["evidence_sufficient"])
        self.assertEqual(result["admitted_frames"], 1)

    def test_unrelated_sequence_produces_no_experimental_match(self):
        matcher = StreamingAtlasMatcher(build_atlas(self.frames))
        for frame in synthetic_sweep(999):
            self.assertFalse(matcher.observe(frame)["evidence_sufficient"])

    def test_features_are_extracted_once_per_live_frame(self):
        matcher = StreamingAtlasMatcher(build_atlas(self.frames))
        features = matcher.tracker.features
        with mock.patch.object(features, "detect_features", wraps=features.detect_features) as detect:
            matcher.observe(self.frames[0])
        self.assertEqual(detect.call_count, 1)

    def test_inconsistent_atlas_pose_cannot_add_apparent_new_region(self):
        atlas = build_atlas(self.frames)
        matcher = StreamingAtlasMatcher(atlas)
        matcher.observe(self.frames[0])
        shift = np.float32([[1, 0, 60], [0, 1, 0], [0, 0, 1]])
        for keyframe in atlas.keyframes:
            old = keyframe.observation.registration
            keyframe.observation.registration = replace(old, transform=shift @ old.transform)
        result = matcher.observe(self.frames[1])
        self.assertEqual(result["reason"], "inconsistent_pose")
        self.assertEqual(result["admitted_frames"], 0)
        self.assertFalse(result["evidence_sufficient"])

    def test_private_atlas_round_trip_preserves_frames_features_and_evidence(self):
        atlas = build_atlas(self.frames)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "atlas"
            save_atlas(atlas, directory, [])
            restored, _ = load_atlas(directory)
            self.assertEqual(atlas.summary(), restored.summary())
            for old, new in zip(atlas.keyframes, restored.keyframes):
                self.assertEqual(old.observation.raw_frame, new.observation.raw_frame)
                np.testing.assert_array_equal(old.observation.descriptors, new.observation.descriptors)
                np.testing.assert_array_equal(old.observation.registration.transform,
                                              new.observation.registration.transform)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            for file in directory.iterdir():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            matcher = StreamingAtlasMatcher(restored)
            matcher.observe(self.frames[0])
            self.assertTrue(matcher.observe(self.frames[1])["evidence_sufficient"])

    def test_atlas_checksum_and_version_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "atlas"
            manifest = save_atlas(build_atlas(self.frames), directory, [])
            original = dict(manifest)
            manifest["matcher_version"] = "another-matcher"
            (directory / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "re-enroll"):
                load_atlas(directory)
            (directory / "manifest.json").write_text(json.dumps(original))
            with (directory / "atlas.npz").open("ab") as stream:
                stream.write(b"corruption")
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_atlas(directory)

    def test_sequence_enrollment_and_probe_report_end_to_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record(root / "enrollment", self.frames)
            record(root / "probe", [self.frames[0], self.frames[0], self.frames[1]], "development")
            atlas, sources = enroll_sequences([root / "enrollment"])
            manifest = save_atlas(atlas, root / "atlas", sources)
            restored, loaded_manifest = load_atlas(root / "atlas")
            report = evaluate_sequence(restored, loaded_manifest, root / "probe", "genuine")
            self.assertTrue(report["candidate_match"])
            self.assertTrue(report["candidate_correct"])
            self.assertFalse(report["promotable"])
            self.assertAlmostEqual(report["time_to_evidence_capture_ms"], 2100)
            with self.assertRaisesRegex(ValueError, "cannot serve"):
                evaluate_sequence(atlas, manifest, root / "enrollment", "genuine")
            with self.assertRaisesRegex(ValueError, "labeled enrollment"):
                enroll_sequences([root / "probe"])

    def test_truncated_recording_is_rejected_before_feature_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "touch"
            record(directory, self.frames)
            path = directory / "frames.bin"
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError, "corrupt"):
                load_sequence(directory)

    def test_incomplete_probe_has_no_correctness_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record(root / "enrollment", self.frames)
            record(root / "probe", [self.frames[0], self.frames[1]], "development")
            atlas, sources = enroll_sequences([root / "enrollment"])
            saved = save_atlas(atlas, root / "atlas", sources)
            path = root / "probe" / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["complete"] = False
            path.write_text(json.dumps(manifest))
            report = evaluate_sequence(atlas, saved, root / "probe", "genuine")
            self.assertFalse(report["valid_trial"])
            self.assertIsNone(report["candidate_correct"])

    def test_clean_timeout_enrollment_is_replayable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record(root / "enrollment", self.frames)
            path = root / "enrollment" / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["complete"] = False
            path.write_text(json.dumps(manifest))
            atlas, sources = enroll_sequences(
                [root / "enrollment"], finger="right-index-finger")
            self.assertTrue(atlas.keyframes)

    def test_probe_finger_metadata_enforces_genuine_and_impostor_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record(root / "enrollment", self.frames, finger="right-index-finger")
            record(root / "probe", synthetic_sweep(99), "development", "right-thumb")
            atlas, sources = enroll_sequences(
                [root / "enrollment"], finger="right-index-finger")
            saved = save_atlas(
                atlas, root / "atlas", sources, finger="right-index-finger")
            with self.assertRaisesRegex(ValueError, "genuine probe"):
                evaluate_sequence(atlas, saved, root / "probe", "genuine")
            report = evaluate_sequence(atlas, saved, root / "probe", "impostor")
            self.assertEqual(report["probe_finger"], "right-thumb")


if __name__ == "__main__":
    unittest.main()
