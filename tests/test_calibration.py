import unittest

import numpy as np

from egis_matcher.calibration import (
    CalibrationProfile, FramePreprocessor, UnsupportedCorrection,
)
from egis_matcher.frame import FrameSpec


class CalibrationTests(unittest.TestCase):
    def profile(self, **changes):
        values = {
            "frame_spec": FrameSpec(3, 2),
            "background": bytes([20, 20, 20, 100, 100, 100]),
            "bad_pixels": bytes(6),
            "provenance": ("ghidra:EgisTouchFP0575:11484",),
        }
        values.update(changes)
        return CalibrationProfile(**values)

    def test_profile_round_trip_and_digest_are_deterministic(self):
        profile = self.profile(sensor_gain=12)
        restored = CalibrationProfile.from_dict(profile.to_dict())
        self.assertEqual(restored, profile)
        self.assertEqual(restored.digest, profile.digest)

    def test_diagnostics_use_confirmed_background_minus_raw_direction(self):
        result = FramePreprocessor(self.profile()).diagnose(
            bytes([10, 20, 30, 80, 100, 120]))
        np.testing.assert_array_equal(
            result.signed_background_difference,
            [[10, 0, -10], [20, 0, -20]],
        )

    def test_unknown_conversion_fails_closed(self):
        with self.assertRaisesRegex(UnsupportedCorrection, "conversion"):
            FramePreprocessor(self.profile()).correct(bytes(6))

    def test_confirmed_lut_is_applied_without_hidden_normalization(self):
        lut = bytes(min(255, max(0, index - 255 + 128)) for index in range(511))
        corrected = FramePreprocessor(self.profile(difference_lut=lut)).correct(
            bytes([10, 20, 30, 80, 100, 120]))
        np.testing.assert_array_equal(corrected, [[138, 128, 118], [148, 128, 108]])
        self.assertFalse(corrected.flags.writeable)

    def test_bad_pixels_prevent_unconfirmed_replacement(self):
        lut = bytes(range(256)) + bytes(range(255))
        with self.assertRaisesRegex(UnsupportedCorrection, "bad-pixel"):
            FramePreprocessor(self.profile(
                difference_lut=lut, bad_pixels=bytes([0, 1, 0, 0, 0, 0]))).correct(bytes(6))

    def test_wrong_geometry_and_nonbinary_map_are_rejected(self):
        with self.assertRaises(ValueError):
            self.profile(background=bytes(5))
        with self.assertRaises(ValueError):
            self.profile(bad_pixels=bytes([0, 0, 2, 0, 0, 0]))


if __name__ == "__main__":
    unittest.main()
