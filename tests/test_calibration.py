import unittest

import numpy as np

from egis_matcher.calibration import (
    CalibrationProfile, FramePreprocessor, UnsupportedCorrection,
    WINDOWS_DIFFERENCE_CONVERSION_PROVENANCE, windows_difference_lut,
)
from egis_matcher.frame import FrameSpec


def _int32(value):
    return (value + 2 ** 31) % 2 ** 32 - 2 ** 31


def _slot_0x120(edx, gain, offset, high, low):
    """Instruction-level transcription of EgisTouchFP0575 0x18001c870."""
    edx = _int32(edx - offset)                      # sub edx,[rcx+0x1c]
    if edx < 0:                                     # jns
        return 0xff
    eax = _int32(gain * edx + 8)                    # imul eax,edx; add eax,8
    eax = _int32(eax + ((eax >> 31) & 0xf)) >> 4    # cdq; and edx,0xf; add; sar 4
    eax = _int32(eax * 0xff)
    ecx = _int32(high - low)
    quotient = abs(eax) // abs(ecx)                 # idiv truncates toward zero
    eax = _int32((quotient if (eax < 0) == (ecx < 0) else -quotient) - low)
    ecx = 0xff if eax > 0xff else eax               # cmovg
    ecx = 0 if eax < 0 else ecx                     # cmovs
    return (0xff - ecx) & 0xff


class WindowsDifferenceConversionTests(unittest.TestCase):
    def test_default_lut_matches_instruction_transcription(self):
        lut = windows_difference_lut()
        self.assertEqual(len(lut), 511)
        self.assertEqual(
            list(lut), [_slot_0x120(d, 48, 0, 128, 0) for d in range(-255, 256)])

    def test_default_lut_shape(self):
        lut = windows_difference_lut()
        at = lambda difference: lut[difference + 255]
        self.assertTrue(all(at(d) == 255 for d in range(-255, 1)))
        self.assertEqual([at(d) for d in (1, 2, 21, 42)], [250, 244, 130, 4])
        self.assertTrue(all(at(d) == 0 for d in range(43, 256)))

    def test_nondefault_fields_match_instruction_transcription(self):
        for fields in ({"gain": 16, "offset": 5, "high": 200, "low": 20},
                       {"gain": 100, "offset": -10, "high": 64, "low": -32},
                       {"gain": 7, "offset": 0, "high": 0, "low": 255}):
            with self.subTest(**fields):
                self.assertEqual(
                    list(windows_difference_lut(**fields)),
                    [_slot_0x120(d, fields["gain"], fields["offset"],
                                 fields["high"], fields["low"]) for d in range(-255, 256)])

    def test_empty_range_is_rejected(self):
        with self.assertRaises(ValueError):
            windows_difference_lut(high=50, low=50)

    def test_profile_with_recovered_lut_corrects_frames(self):
        profile = CalibrationProfile(
            frame_spec=FrameSpec(3, 2),
            background=bytes([20, 20, 20, 100, 100, 100]),
            bad_pixels=bytes(6),
            difference_lut=windows_difference_lut(),
            provenance=(WINDOWS_DIFFERENCE_CONVERSION_PROVENANCE,),
        )
        self.assertTrue(profile.correction_supported)
        corrected = FramePreprocessor(profile).correct(bytes([10, 20, 30, 80, 100, 120]))
        np.testing.assert_array_equal(corrected, [[196, 255, 255], [136, 255, 255]])
        self.assertEqual(CalibrationProfile.from_dict(profile.to_dict()), profile)


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
