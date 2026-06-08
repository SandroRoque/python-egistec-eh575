import json
import os
import tempfile
import unittest

import cv2
import numpy as np

from egis_driver import fingerprint_matcher
from egis_driver.persistence import Persistence


class FingerprintMatcherStorageTests(unittest.TestCase):
    def _persistence(self, root_dir):
        persistence = Persistence(root_dir)
        persistence.ensure_dirs()
        return persistence

    def _matcher(self, root_dir):
        return fingerprint_matcher.FingerprintMatcher(
            persistence=self._persistence(root_dir),
        )

    def test_unvalidated_threshold_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = self._persistence(tmp)
            threshold_file = os.path.join(persistence.calibration_dir, "thresholds.json")
            with open(threshold_file, "w") as f:
                json.dump({
                    "matcher_version": fingerprint_matcher.MATCHER_VERSION,
                    "thresholds": {
                        "min_inliers": 1,
                        "min_inlier_ratio": 0.1,
                        "min_inlier_frames": 1,
                        "min_frame_inliers": 1,
                        "min_margin": 0.0,
                        "min_ncc": 0.0,
                        "min_orientation": 0.0,
                        "min_ridge_score": 0.0,
                    },
                }, f)

            matcher = fingerprint_matcher.FingerprintMatcher(persistence=persistence)

            self.assertFalse(matcher.calibrated)

    def test_legacy_template_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = self._persistence(tmp)
            kp = [(cv2.KeyPoint(x=1, y=1, size=1).pt, 1, -1, 0, 0, -1)]
            des = np.ones((4, 128), dtype=np.float32)
            np.save(
                os.path.join(persistence.enroll_dir, "testuser_right-index-finger.npy"),
                np.array([(kp, des)], dtype=object),
            )

            matcher = fingerprint_matcher.FingerprintMatcher(persistence=persistence)

            self.assertIsNone(matcher.train_descriptors)
            self.assertEqual(matcher.get_enrolled_fingers("testuser"), [])
            self.assertEqual(matcher.legacy_templates, ["testuser_right-index-finger.npy"])

    def test_v4_template_loads_into_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = self._persistence(tmp)
            matcher = fingerprint_matcher.FingerprintMatcher(persistence=persistence)
            img = np.zeros((52, 103), dtype=np.uint8)
            cv2.line(img, (8, 8), (95, 44), 255, 2)
            kp_points = [
                cv2.KeyPoint(x=10, y=10, size=2),
                cv2.KeyPoint(x=30, y=18, size=2),
                cv2.KeyPoint(x=50, y=26, size=2),
                cv2.KeyPoint(x=70, y=34, size=2),
            ]
            ridge = matcher.features.template_descriptor(img)

            base = "testuser_right-index-finger"
            json_path = os.path.join(persistence.enroll_dir, base + ".json")
            npz_path = os.path.join(persistence.enroll_dir, base + ".npz")

            meta = {
                "schema_version": fingerprint_matcher.TEMPLATE_SCHEMA_VERSION,
                "matcher_version": fingerprint_matcher.MATCHER_VERSION,
                "name": base,
                "created_at": 0,
            }
            with open(json_path, "w") as f:
                json.dump(meta, f)

            kp_arr = np.array([
                [p.pt[0], p.pt[1], p.size, p.angle, p.response, p.octave, p.class_id]
                for p in kp_points
            ], dtype=np.float32)
            orient = ridge["orientation"]
            np.savez(npz_path,
                num_templates=np.array(1, dtype=np.int32),
                kp_0=kp_arr,
                desc_0=np.ones((4, 128), dtype=np.float32),
                img_0=img,
                ridge_cos2_0=orient["cos2"].astype(np.float32),
                ridge_sin2_0=orient["sin2"].astype(np.float32),
                ridge_weight_0=orient["weight"].astype(np.float32),
                quality_0=np.float32(0.5),
            )

            matcher.rebuild_index()

            self.assertIsNotNone(matcher.train_descriptors)
            self.assertEqual(matcher.get_enrolled_fingers("testuser"), ["right-index-finger"])

    def test_any_finger_is_identification_not_literal_template_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            matcher = self._matcher(tmp)
            default_thresholds = matcher._default_thresholds()
            specific_thresholds = dict(default_thresholds)
            specific_thresholds["min_inliers"] = default_thresholds["min_inliers"] + 10
            matcher.thresholds_by_target = {
                "testuser/right-index-finger": specific_thresholds,
            }

            self.assertIsNone(matcher._normalize_verify_finger("any"))
            self.assertIsNone(matcher._normalize_verify_finger(""))
            self.assertEqual(
                matcher._normalize_verify_finger("right-index-finger"),
                "right-index-finger",
            )
            self.assertEqual(
                matcher._active_thresholds(
                    "testuser",
                    matcher._normalize_verify_finger("any"),
                ),
                default_thresholds,
            )


if __name__ == "__main__":
    unittest.main()
