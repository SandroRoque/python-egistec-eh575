import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "open-fprintd-eh575/bin/egis-enroll"


def load_script():
    loader = SourceFileLoader("egis_enroll", str(SCRIPT))
    spec = importlib.util.spec_from_loader("egis_enroll", loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuidedEnrollTests(unittest.TestCase):
    def test_sensor_prompts_name_lift_and_detected_release(self):
        module = load_script()
        output = []
        module.render_sensor_prompt("lift-finger", output=output.append)
        module.render_sensor_prompt("place-finger", output=output.append)
        rendered = "\n".join(output)
        self.assertIn("LIFT YOUR FINGER COMPLETELY", rendered)
        self.assertIn("Release detected", rendered)
        self.assertIn("PLACE THE SAME FINGER AGAIN", rendered)

    def test_finger_name_is_required_and_validated(self):
        module = load_script()
        args = module.parse_args(["-f", "left-thumb", "alice"])
        self.assertEqual(args.finger, "left-thumb")
        self.assertEqual(args.username, "alice")


if __name__ == "__main__":
    unittest.main()
