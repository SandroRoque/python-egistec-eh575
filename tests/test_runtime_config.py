import os
import unittest
from unittest import mock
from pathlib import Path

from egis_driver.runtime_config import RuntimePaths


class RuntimeConfigTests(unittest.TestCase):
    def test_match_mode_defaults_to_shadow(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(RuntimePaths.from_environment().match_mode, "shadow")

    def test_match_mode_is_validated(self):
        with self.assertRaisesRegex(ValueError, "EGIS_MATCH_MODE"):
            RuntimePaths(match_mode="unsafe-fallback")

        with self.assertRaisesRegex(ValueError, "EGIS_MATCH_MODE"):
            RuntimePaths(match_mode="touch")

    def test_data_root_controls_every_biometric_directory(self):
        paths = RuntimePaths(data_root=Path("/tmp/offline"))
        self.assertEqual(paths.enrollment_dir, Path("/tmp/offline/egis"))
        self.assertEqual(paths.calibration_dir, Path("/tmp/offline/egis-calibration"))
        self.assertEqual(paths.atlas_dir, Path("/tmp/offline/egis-atlas"))
        self.assertEqual(paths.gallery_dir, Path("/tmp/offline/egis-gallery"))


if __name__ == "__main__":
    unittest.main()
