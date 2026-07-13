import importlib.machinery
import importlib.util
import unittest


def load_calibrate_module():
    loader = importlib.machinery.SourceFileLoader(
        "egis_calibrate",
        "open-fprintd-eh575/bin/egis-calibrate",
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CalibrationThresholdTests(unittest.TestCase):
    def test_impostor_samples_do_not_tune_identity_policy(self):
        calibrate = load_calibrate_module()
        records = [
            {
                "username": "testuser",
                "target_finger": "right-index-finger",
                "label": "genuine",
                "best": {
                    "inliers": 8,
                    "inlier_ratio": 0.5,
                    "inlier_frames": 1,
                    "max_frame_inliers": 8,
                    "margin": 2.0,
                    "ncc": 0.50,
                    "orientation": 0.90,
                    "ridge_score": 0.70,
                },
            },
            {
                "username": "testuser",
                "target_finger": "right-index-finger",
                "label": "genuine",
                "best": {
                    "inliers": 8,
                    "inlier_ratio": 0.5,
                    "inlier_frames": 1,
                    "max_frame_inliers": 8,
                    "margin": 2.0,
                    "ncc": 0.90,
                    "orientation": 0.75,
                    "ridge_score": 0.70,
                },
            },
            {
                "username": "testuser",
                "target_finger": "right-index-finger",
                "label": "impostor",
                "best": {
                    "inliers": 5,
                    "inlier_ratio": 0.5,
                    "inlier_frames": 1,
                    "max_frame_inliers": 5,
                    "margin": 1.0,
                    "ncc": 0.85,
                    "orientation": 0.10,
                    "ridge_score": 0.40,
                },
            },
            {
                "username": "testuser",
                "target_finger": "right-index-finger",
                "label": "impostor",
                "best": {
                    "inliers": 5,
                    "inlier_ratio": 0.5,
                    "inlier_frames": 1,
                    "max_frame_inliers": 5,
                    "margin": 1.0,
                    "ncc": 0.10,
                    "orientation": 0.85,
                    "ridge_score": 0.40,
                },
            },
        ]

        thresholds, by_target, warnings, validation = calibrate.recommend_thresholds(
            records,
            min_genuine_samples=2,
            min_impostor_samples=2,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(thresholds, calibrate.IDENTITY_POLICY)
        self.assertEqual(by_target, {})
        self.assertEqual(validation["genuine_pass"], 2)
        self.assertEqual(validation["impostor_pass"], 0)
        self.assertEqual(validation["policy"], "fixed_positive_identity")
        self.assertEqual(
            validation["per_target"]["testuser/right-index-finger"]["genuine_pass"],
            2,
        )

    def test_every_target_uses_same_global_identity_policy(self):
        calibrate = load_calibrate_module()
        records = [
            {
                "username": "testuser",
                "target_finger": "right-index-finger",
                "label": "genuine",
                "best": {
                    "inliers": 8,
                    "inlier_ratio": 0.8,
                    "inlier_frames": 1,
                    "max_frame_inliers": 8,
                    "margin": 4.0,
                    "ncc": 0.70,
                    "orientation": 0.80,
                    "ridge_score": 0.70,
                },
            },
            {
                "username": "testuser",
                "target_finger": "right-index-finger",
                "label": "impostor",
                "best": {
                    "inliers": 5,
                    "inlier_ratio": 0.5,
                    "inlier_frames": 1,
                    "max_frame_inliers": 5,
                    "margin": 0.5,
                    "ncc": 0.50,
                    "orientation": 0.70,
                    "ridge_score": 0.50,
                },
            },
            {
                "username": "testuser",
                "target_finger": "right-thumb",
                "label": "genuine",
                "best": {
                    "inliers": 8,
                    "inlier_ratio": 0.8,
                    "inlier_frames": 1,
                    "max_frame_inliers": 8,
                    "margin": 4.0,
                    "ncc": 0.70,
                    "orientation": 0.80,
                    "ridge_score": 0.70,
                },
            },
            {
                "username": "testuser",
                "target_finger": "right-thumb",
                "label": "impostor",
                "best": {},
            },
        ]

        thresholds, by_target, warnings, validation = calibrate.recommend_thresholds(
            records,
            min_genuine_samples=1,
            min_impostor_samples=1,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(by_target, {})
        self.assertTrue(validation["per_target"]["testuser/right-index-finger"]["uses_global_thresholds"])
        self.assertTrue(validation["per_target"]["testuser/right-thumb"]["uses_global_thresholds"])
        self.assertEqual(validation["per_target"]["testuser/right-thumb"]["thresholds"], thresholds)

    def test_unscored_genuine_samples_count_as_failed_validation_attempts(self):
        calibrate = load_calibrate_module()
        passing = {
            "inliers": 8,
            "inlier_ratio": 0.8,
            "inlier_frames": 1,
            "max_frame_inliers": 8,
            "margin": 4.0,
            "ncc": 0.70,
            "orientation": 0.80,
            "ridge_score": 0.70,
        }
        records = []
        for index in range(8):
            records.append({
                "username": "testuser",
                "target_finger": "right-index-finger",
                "label": "genuine",
                "best": dict(passing) if index < 3 else {},
            })
        for _ in range(8):
            records.append({
                "username": "testuser",
                "target_finger": "right-index-finger",
                "label": "impostor",
                "best": {},
            })

        _, _, warnings, validation = calibrate.recommend_thresholds(records)

        self.assertTrue(any("3/8 collected genuine samples" in item for item in warnings))
        self.assertEqual(validation["genuine_pass"], 3)
        self.assertEqual(validation["genuine_scored"], 3)
        self.assertEqual(
            validation["per_target"]["testuser/right-index-finger"]["genuine_pass_required"],
            6,
        )


if __name__ == "__main__":
    unittest.main()
