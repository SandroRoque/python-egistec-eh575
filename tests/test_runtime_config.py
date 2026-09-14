import os
import unittest
from unittest import mock

from egis_driver.runtime_config import RuntimePaths


class RuntimeConfigTests(unittest.TestCase):
    def test_match_mode_defaults_to_shadow(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(RuntimePaths.from_environment().match_mode, "shadow")

    def test_match_mode_is_validated(self):
        with self.assertRaisesRegex(ValueError, "EGIS_MATCH_MODE"):
            RuntimePaths(match_mode="unsafe-fallback")


if __name__ == "__main__":
    unittest.main()
