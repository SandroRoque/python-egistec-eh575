import base64
import unittest

import numpy as np

from egis_matcher.sourceafis import SourceAfisEngine, SourceAfisTemplate
from egis_matcher.stitching import StitchedPrint


class FakeSourceAfisEngine(SourceAfisEngine):
    def __init__(self, **kwargs):
        super().__init__(home="/unused", **kwargs)
        self.commands = []

    def _start(self):
        pass

    def _exchange(self, command):
        self.commands.append(command)
        if command.startswith("EXTRACT\t"):
            return "TEMPLATE\t" + base64.b64encode(b"opaque").decode("ascii")
        if command.startswith("COMPARE\t"):
            return "SCORE\t42.5"
        return "PONG\t3.18.1"


class SourceAfisEngineTests(unittest.TestCase):
    def test_extract_transports_scaled_grayscale_and_returns_opaque_template(self):
        engine = FakeSourceAfisEngine(scale=2, invert=True)
        image = np.full((4, 5), 20, dtype=np.uint8)
        mask = np.full(image.shape, 255, dtype=np.uint8)
        result = engine.extract(StitchedPrint(image, mask, 20, 2, 0, .8))
        self.assertEqual(result.reason, "ok")
        self.assertEqual(result.template.data, b"opaque")
        fields = engine.commands[0].split("\t")
        self.assertEqual(fields[1:4], ["10", "8", "500"])
        self.assertEqual(set(base64.b64decode(fields[4])), {235})

    def test_compare_uses_only_opaque_templates(self):
        engine = FakeSourceAfisEngine()
        score = engine.compare(SourceAfisTemplate(b"one"), SourceAfisTemplate(b"two"))
        self.assertEqual(score, 42.5)

    def test_extract_fails_closed_on_worker_error(self):
        engine = FakeSourceAfisEngine()
        engine._exchange = lambda command: (_ for _ in ()).throw(RuntimeError("bad"))
        image = np.zeros((4, 4), dtype=np.uint8)
        result = engine.extract(StitchedPrint(image, image, 0, 1, 0, 0))
        self.assertIsNone(result.template)
        self.assertEqual(result.reason, "extraction_failed:RuntimeError")

    def test_template_size_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "template size"):
            SourceAfisTemplate(b"")


if __name__ == "__main__":
    unittest.main()
