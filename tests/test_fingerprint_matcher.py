import json
import os
import tempfile
import unittest

import cv2
import numpy as np

from egis_driver import fingerprint_matcher
from egis_driver.identity_matcher import IdentityMatcher
from egis_driver.matcher_config import MatcherConfig
from egis_driver.persistence import Persistence
from egis_driver.template_builder import TemplateBuilder


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

    def test_previous_matcher_threshold_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = self._persistence(tmp)
            threshold_file = os.path.join(persistence.calibration_dir, "thresholds.json")
            with open(threshold_file, "w") as f:
                json.dump({
                    "matcher_version": fingerprint_matcher.MATCHER_VERSION - 1,
                    "validated": True,
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

    def test_username_index_excludes_other_users_before_matching(self):
        with tempfile.TemporaryDirectory() as tmp:
            matcher = fingerprint_matcher.FingerprintMatcher(
                persistence=self._persistence(tmp),
                matcher_config=MatcherConfig(index_scope="username"),
            )
            matcher.train_descriptors = np.arange(6 * 128, dtype=np.float32).reshape(6, 128)
            matcher.descriptor_lookup = {
                0: ("alice_right-index-finger.npz", 0, 0),
                1: ("alice_right-index-finger.npz", 0, 1),
                2: ("bob_right-index-finger.npz", 0, 0),
                3: ("bob_right-index-finger.npz", 0, 1),
                4: ("alice_right-thumb.npz", 0, 0),
                5: ("alice_right-thumb.npz", 0, 1),
            }

            descriptors, lookup, flann, scope = matcher._verification_index("alice")

            self.assertEqual(scope, "username")
            self.assertEqual(descriptors.shape, (4, 128))
            self.assertIsNotNone(flann)
            self.assertEqual(
                {owner[0] for owner in lookup.values()},
                {"alice_right-index-finger.npz", "alice_right-thumb.npz"},
            )


class IdentityMatcherCandidateTests(unittest.TestCase):
    def _record(self, filename, score):
        return {
            "filename": filename,
            "name": filename.removesuffix(".npz"),
            "score": float(score),
            "inliers": int(score),
            "ridge_score": 0.8,
        }

    def test_same_finger_templates_do_not_compete_in_margin(self):
        matcher = IdentityMatcher()
        records = [
            self._record("alice_right-index-finger.npz", 20),
            self._record("alice_right-index-finger.npz", 19),
            self._record("alice_right-thumb.npz", 7),
        ]

        best = matcher._select_best(records, "alice_right-index-finger.npz")

        self.assertEqual(best["margin"], 13.0)
        self.assertEqual(best["competitor"]["name"], "alice_right-thumb")

    def test_nonviable_other_finger_does_not_create_identity_conflict(self):
        matcher = IdentityMatcher()
        thresholds = {
            "min_inliers": 5,
            "min_inlier_ratio": 0.35,
            "min_inlier_frames": 1,
            "min_frame_inliers": 5,
            "min_ncc": 0.28,
            "min_orientation": 0.45,
            "min_ridge_score": 0.45,
        }
        target = self._record("alice_right-thumb.npz", 6)
        target.update({
            "inlier_ratio": 0.6,
            "inlier_frames": 1,
            "max_frame_inliers": 6,
            "ncc": 0.6,
            "orientation": 0.7,
        })
        competitor = self._record("alice_right-index-finger.npz", 12)
        competitor.update({
            "inlier_ratio": 0.8,
            "inlier_frames": 1,
            "max_frame_inliers": 12,
            "ncc": 0.8,
            "orientation": 0.2,
        })

        best = matcher._select_best(
            [competitor, target],
            "alice_right-thumb.npz",
            thresholds,
        )

        self.assertEqual(best["filename"], "alice_right-thumb.npz")
        self.assertEqual(best["margin"], 6.0)
        self.assertNotIn("competitor", best)

    def test_candidate_slots_include_distinct_enrolled_fingers(self):
        matcher = IdentityMatcher(config=MatcherConfig(max_candidates=3))
        votes = {
            ("alice_right-index-finger.npz", 0): [None] * 12,
            ("alice_right-index-finger.npz", 1): [None] * 11,
            ("alice_right-index-finger.npz", 2): [None] * 10,
            ("alice_right-thumb.npz", 0): [None] * 6,
        }

        selected = matcher._select_top_candidates(votes)

        self.assertIn(("alice_right-thumb.npz", 0), [key for key, _ in selected])
        self.assertEqual(len(selected), 3)


class TemplateBuilderTouchDiversityTests(unittest.TestCase):
    def test_selection_takes_one_frame_per_touch_before_extra_frames(self):
        builder = TemplateBuilder()
        frame = np.zeros((52, 103), dtype=np.uint8)
        groups = [
            [(frame.copy(), 10.0), (frame.copy(), 9.0)],
            [(frame.copy(), 20.0), (frame.copy(), 19.0)],
            [(frame.copy(), 30.0), (frame.copy(), 29.0)],
        ]

        selected = builder._remove_duplicates_by_touch(
            groups,
            max_frames=3,
            ssim_threshold=1.1,
        )

        self.assertEqual([quality for _, quality in selected], [10.0, 20.0, 30.0])

    def test_nested_frames_are_preserved_as_touch_groups(self):
        builder = TemplateBuilder()
        frame = bytes(52 * 103)

        groups = builder._normalize_touch_groups([[frame, frame], [frame]])

        self.assertEqual([len(group) for group in groups], [2, 1])


if __name__ == "__main__":
    unittest.main()
