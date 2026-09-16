import importlib.util
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def load_inspector():
    loader = SourceFileLoader("egis_inspect", str(ROOT / "egis-inspect"))
    spec = importlib.util.spec_from_loader("egis_inspect", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class InspectorStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inspector = load_inspector()

    def test_private_directory_and_png_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "captures"
            returned = self.inspector.ensure_private_directory(directory)
            self.assertEqual(returned, directory)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

            output = directory / "frame.png"
            self.inspector.write_private_png(
                output, np.zeros((4, 4), dtype=np.uint8))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

            with self.assertRaises(FileExistsError):
                self.inspector.write_private_png(
                    output, np.zeros((4, 4), dtype=np.uint8))

    def test_symlinked_and_open_directories_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            open_directory = root / "open"
            open_directory.mkdir(mode=0o755)
            open_directory.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "not private"):
                self.inspector.ensure_private_directory(open_directory)

            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                self.inspector.ensure_private_directory(link)


if __name__ == "__main__":
    unittest.main()
