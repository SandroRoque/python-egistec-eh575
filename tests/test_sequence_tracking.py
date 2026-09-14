import unittest

import cv2
import numpy as np

from egis_matcher.frame import FrameSpec
from egis_matcher.sequence import TouchTracker
from egis_matcher.stitching import TouchStitcher


class SequenceTrackingTests(unittest.TestCase):
    def setUp(self):
        self.spec = FrameSpec(103, 52)
        rng = np.random.default_rng(42)
        image = rng.integers(0, 256, (52, 103), dtype=np.uint8)
        cv2.line(image, (8, 8), (90, 40), 255, 2)
        cv2.circle(image, (50, 25), 12, 0, 2)
        self.image = image

    def test_related_frames_share_component_and_render_mosaic(self):
        tracker = TouchTracker(
            self.spec, min_inliers=4, min_spatial_cells=2,
            min_ridge_score=-1.0,
        )
        first = tracker.observe(self.image.tobytes(), 1)
        shifted = cv2.warpAffine(
            self.image, np.float32([[1, 0, 4], [0, 1, 2]]), (103, 52))
        second = tracker.observe(shifted.tobytes(), 2)
        self.assertEqual(first.registration.component,
                         second.registration.component)
        self.assertIsNotNone(second.registration.reference)
        mosaic = tracker.render_components()[0]
        self.assertGreaterEqual(mosaic.shape[0], self.spec.height)
        self.assertGreaterEqual(mosaic.shape[1], self.spec.width)

    def test_discontinuity_forces_a_new_component(self):
        tracker = TouchTracker(self.spec)
        first = tracker.observe(self.image.tobytes(), 1)
        tracker.discontinuity()
        second = tracker.observe(self.image.tobytes(), 3)
        self.assertNotEqual(first.registration.component,
                            second.registration.component)
        self.assertEqual(tracker.summary()["discontinuities"], 1)

    def test_later_frames_cannot_reconnect_across_a_discontinuity(self):
        tracker = TouchTracker(self.spec)
        tracker.observe(self.image.tobytes(), 1)
        tracker.discontinuity()
        tracker.observe(bytes(self.spec.byte_count), 3)
        later = tracker.observe(self.image.tobytes(), 4)
        self.assertIsNone(later.registration.reference)
        self.assertNotEqual(later.registration.component, 0)

    def test_stitcher_masks_sensor_edges_and_grows_canvas(self):
        tracker = TouchTracker(
            self.spec, min_inliers=4, min_spatial_cells=2,
            min_ridge_score=-1.0)
        stitcher = TouchStitcher(self.spec, tracker=tracker, border=4)
        stitcher.begin_touch()
        stitcher.observe(self.image.tobytes(), 1)
        shifted = cv2.warpAffine(
            self.image, np.float32([[1, 0, 8], [0, 1, 0]]), (103, 52))
        stitcher.observe(shifted.tobytes(), 2)
        result = stitcher.snapshot(force=True)
        self.assertIsNotNone(result)
        self.assertGreater(result.coverage_pixels, 0)
        self.assertEqual(result.image.shape, result.mask.shape)
        self.assertTrue(np.all(result.image[result.mask == 0] == 255))

    def test_stitcher_does_not_join_discontinuous_components(self):
        stitcher = TouchStitcher(self.spec)
        stitcher.begin_touch()
        stitcher.observe(self.image.tobytes(), 1)
        stitcher.discontinuity()
        stitcher.observe(self.image.tobytes(), 2)
        result = stitcher.snapshot(force=True)
        self.assertEqual(result.admitted_frames, 1)

    def test_winner_blend_preserves_a_source_pixel(self):
        stitcher = TouchStitcher(self.spec, blend="winner", border=4)
        stitcher.begin_touch()
        stitcher.observe(self.image.tobytes(), 1)
        result = stitcher.snapshot(force=True)
        self.assertGreater(np.unique(result.image[result.mask > 0]).size, 1)

    def test_median_blend_produces_a_masked_composite(self):
        stitcher = TouchStitcher(self.spec, blend="median", border=4)
        stitcher.begin_touch()
        stitcher.observe(self.image.tobytes(), 1)
        result = stitcher.snapshot(force=True)
        self.assertEqual(result.image.shape, result.mask.shape)
        self.assertTrue(np.all(result.image[result.mask == 0] == 255))


if __name__ == "__main__":
    unittest.main()
