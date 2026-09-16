import unittest

import numpy as np
from egis_matcher.image_features import ImageFeatureExtractor


class FingerprintFeatureTests(unittest.TestCase):
    def test_sift_features_exclude_physical_sensor_edge(self):
        image = np.random.default_rng(7).integers(
            0, 256, size=(52, 103), dtype=np.uint8)
        keypoints, _ = ImageFeatureExtractor(edge_margin=5).detect_features(image)
        for keypoint in keypoints:
            self.assertGreaterEqual(keypoint.pt[0], 5)
            self.assertLess(keypoint.pt[0], 98)
            self.assertGreaterEqual(keypoint.pt[1], 5)
            self.assertLess(keypoint.pt[1], 47)


if __name__ == "__main__":
    unittest.main()
