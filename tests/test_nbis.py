import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from egis_matcher.nbis import NbisEngine, NbisTemplate
from egis_matcher.stitching import StitchedPrint


class FakeNbisEngine(NbisEngine):
    def _run(self, command, cwd):
        name = Path(command[0]).name
        if name == "cwsq":
            Path(cwd, "probe.wsq").write_bytes(b"wsq")
        elif name == "mindtct":
            Path(cwd, "features.xyt").write_text(
                "\n".join(f"{i} {i + 1} 90 80" for i in range(10)) + "\n")
        return subprocess.CompletedProcess(command, 0, stdout="42\n", stderr="")


class NbisEngineTests(unittest.TestCase):
    def _engine(self, directory):
        for name in ("cwsq", "mindtct", "bozorth3"):
            path = Path(directory, name)
            path.write_text("fixture")
        return FakeNbisEngine(bin_dir=directory)

    def test_extract_uses_private_intermediates_and_parses_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = self._engine(temporary)
            image = np.full((80, 120), 100, dtype=np.uint8)
            mask = np.full(image.shape, 255, dtype=np.uint8)
            result = engine.extract(StitchedPrint(
                image, mask, int(mask.size), 3, 0, 0.9))
            self.assertEqual(result.reason, "ok")
            self.assertEqual(result.template.minutiae, 10)

    def test_compare_rejects_non_numeric_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = self._engine(temporary)
            engine._run = lambda *args: subprocess.CompletedProcess(
                [], 0, stdout="not-a-score", stderr="")
            template = NbisTemplate(b"1 2 3 4\n", 1)
            with self.assertRaisesRegex(ValueError, "BOZORTH3 score"):
                engine.compare(template, template)

    def test_malformed_template_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "malformed"):
            NbisEngine._validate_xyt(b"1 2 3\n")

    def test_stitched_print_copies_and_freezes_biometric_arrays(self):
        image = np.full((4, 4), 100, dtype=np.uint8)
        mask = np.full((4, 4), 255, dtype=np.uint8)
        stitched = StitchedPrint(image, mask, 16, 1, 0, 0.0)
        image[0, 0] = 0
        self.assertEqual(stitched.image[0, 0], 100)
        with self.assertRaises(ValueError):
            stitched.image[0, 0] = 0


if __name__ == "__main__":
    unittest.main()
