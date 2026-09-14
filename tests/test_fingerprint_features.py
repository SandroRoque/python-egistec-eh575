import unittest

import cv2
import numpy as np

from egis_matcher.fingerprint_features import FingerprintFeatureExtractor, FingerprintFeatures, Minutia
from egis_matcher.fingerprint_matcher import FingerprintMatcher
from egis_matcher.image_features import ImageFeatureExtractor


class _FakeExtractor:
    def __init__(self):
        self.frame_spec = type("Spec", (), {"byte_count": 1})()

    def extract(self, value):
        shift = float(value[0])
        scale = 0.2 if value[0] == 2 else 1.0
        points = tuple(Minutia(x * scale + shift, y * scale, 0.1, "ending", 0.9)
                       for x, y in ((10, 10), (25, 15), (40, 30), (55, 35),
                                     (70, 20), (85, 40), (30, 42), (65, 45)))
        image = np.zeros((8, 8), dtype=np.uint8)
        return FingerprintFeatures(image, image, image.astype(np.float32),
                                   np.ones_like(image, dtype=np.float32),
                                   0.12, image, points, 0.9)


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

    def test_extractor_returns_bounded_diagnostics(self):
        image = np.tile(np.arange(103, dtype=np.uint8), (52, 1))
        features = FingerprintFeatureExtractor().extract(image)
        self.assertEqual(features.image.shape, (52, 103))
        self.assertGreaterEqual(features.quality, 0.0)
        self.assertLessEqual(features.quality, 1.0)
        self.assertEqual(features.mask.shape, features.orientation.shape)

    def test_raw_frame_size_is_validated(self):
        with self.assertRaises(ValueError):
            FingerprintFeatureExtractor().extract(b"bad")

    def test_minutiae_are_kept_away_from_capture_boundaries(self):
        image = np.random.default_rng(4).integers(
            0, 256, size=(52, 103), dtype=np.uint8)
        extractor = FingerprintFeatureExtractor(edge_margin=5)
        features = extractor.extract(image)
        distance = cv2.distanceTransform(
            (features.mask > 0).astype(np.uint8), cv2.DIST_L2, 3)
        for minutia in features.minutiae:
            x, y = int(minutia.x), int(minutia.y)
            self.assertGreater(min(x, y, 102 - x, 51 - y), 5)
            self.assertGreater(distance[y, x], 5)

    def test_aligned_minutiae_can_match(self):
        matcher = FingerprintMatcher(extractor=_FakeExtractor(), min_inliers=6)
        template = matcher.build_template([b"\x00"])
        result = matcher.match([b"\x00"], template)
        self.assertTrue(result.matched)
        self.assertGreaterEqual(result.metrics["inliers"], 6)

    def test_implausible_transform_is_rejected(self):
        matcher = FingerprintMatcher(extractor=_FakeExtractor(), min_inliers=6)
        template = matcher.build_template([b"\x02"])
        result = matcher.match([b"\x00"], template)
        self.assertFalse(result.matched)
        self.assertLess(result.metrics["transform_scale"], 0.82)


if __name__ == "__main__":
    unittest.main()
