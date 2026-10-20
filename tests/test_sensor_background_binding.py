import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from egis_driver import fingerprint_matcher
from egis_driver.compatibility import state_report
from egis_driver.device_profile import EH575_PROFILE
from egis_driver.persistence import BACKGROUND_KEY, Persistence, background_mismatch
from egis_driver.runtime_config import RuntimePaths

THRESHOLDS = {
    "min_inliers": 40,
    "min_inlier_ratio": 0.72,
    "min_inlier_frames": 1,
    "min_frame_inliers": 10,
    "min_margin": 25.0,
    "min_ncc": 0.30,
    "min_orientation": 0.35,
    "min_ridge_score": 0.38,
}


def _validated_thresholds():
    return {
        "matcher_version": fingerprint_matcher.MATCHER_VERSION,
        "validated": True,
        "thresholds": dict(THRESHOLDS),
    }


class BackgroundBindingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.persistence = Persistence(self.root)
        self.persistence.ensure_dirs()

    def tearDown(self):
        self._tmp.cleanup()

    def _install_background(self, fill):
        data = bytes([fill]) * EH575_PROFILE.frame.byte_count
        with open(self.persistence.sensor_background_file, "wb") as f:
            f.write(data)

    def _write_template(self, name, meta):
        base = os.path.join(self.persistence.enroll_dir, name)
        np.savez(base + ".npz", num_templates=np.int64(0))
        with open(base + ".json", "w") as f:
            json.dump(meta, f)

    def test_writes_record_current_background(self):
        self.assertIsNone(self.persistence.sensor_background_sha256())
        self._install_background(31)
        digest = self.persistence.sensor_background_sha256()
        self.assertEqual(len(digest), 64)

        self.persistence.save_thresholds(_validated_thresholds())
        self.persistence.save_template("user_right-thumb", {"num_templates": np.int64(0)}, {})
        self.persistence.save_sample("sample", {"frame_0": np.zeros(4, np.uint8)}, {})

        self.assertEqual(self.persistence.load_thresholds()[BACKGROUND_KEY], digest)
        self.assertEqual(
            self.persistence.load_template_metadata("user_right-thumb.npz")[BACKGROUND_KEY],
            digest)
        (_, meta, _), = self.persistence.list_samples()
        self.assertEqual(meta[BACKGROUND_KEY], digest)

    def test_unrecorded_records_never_mismatch(self):
        self.assertFalse(background_mismatch({}, "abc"))
        self.assertFalse(background_mismatch({BACKGROUND_KEY: "abc"}, "abc"))
        self.assertTrue(background_mismatch({BACKGROUND_KEY: None}, "abc"))
        self.assertTrue(background_mismatch({BACKGROUND_KEY: "abc"}, None))

    def test_thresholds_from_other_background_fail_closed(self):
        self._install_background(31)
        self.persistence.save_thresholds(_validated_thresholds())
        matcher = fingerprint_matcher.FingerprintMatcher(persistence=self.persistence)
        self.assertTrue(matcher.calibrated)

        self._install_background(32)
        matcher = fingerprint_matcher.FingerprintMatcher(persistence=self.persistence)
        self.assertFalse(matcher.calibrated)

    def test_templates_from_other_background_require_reenrollment(self):
        self._install_background(31)
        current = self.persistence.sensor_background_sha256()
        schema = fingerprint_matcher.TEMPLATE_SCHEMA_VERSION
        self._write_template("user_right-index-finger",
                             {"schema_version": schema, BACKGROUND_KEY: current})
        self._write_template("user_right-thumb",
                             {"schema_version": schema, BACKGROUND_KEY: None})
        self._write_template("user_left-thumb", {"schema_version": schema})

        matcher = fingerprint_matcher.FingerprintMatcher(persistence=self.persistence)

        self.assertEqual(matcher.legacy_templates, ["user_right-thumb.npz"])


class DoctorBackgroundStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = RuntimePaths(data_root=Path(self._tmp.name))
        self.persistence = Persistence(self._tmp.name)
        self.persistence.ensure_dirs()

    def tearDown(self):
        self._tmp.cleanup()

    def _state(self):
        state = state_report(self.paths)
        return state["sensor_background"], state["background_consistency"]

    def test_reports_absent_background_without_records(self):
        self.assertEqual(self._state(), ("absent", "unavailable"))

    def test_reports_invalid_background(self):
        self.paths.sensor_background_path.write_bytes(b"\x00" * 10)
        self.assertEqual(self._state(), ("invalid", "unavailable"))

    def test_reports_consistency_of_records(self):
        self.paths.sensor_background_path.write_bytes(
            b"\x1f" * EH575_PROFILE.frame.byte_count)
        threshold_file = self.paths.calibration_dir / "thresholds.json"
        threshold_file.write_text(json.dumps(_validated_thresholds()))
        self.assertEqual(self._state(), ("installed", "unrecorded"))

        self.persistence.save_thresholds(_validated_thresholds())
        self.assertEqual(self._state(), ("installed", "consistent"))

        self.paths.sensor_background_path.write_bytes(
            b"\x20" * EH575_PROFILE.frame.byte_count)
        self.assertEqual(self._state(), ("installed", "mismatch"))


if __name__ == "__main__":
    unittest.main()
