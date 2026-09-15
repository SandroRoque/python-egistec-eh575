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
    def test_stage_prompt_requires_acknowledgement_and_names_next_action(self):
        module = load_script()
        output = []
        prompts = []

        module.confirm_release(
            3,
            input_fn=lambda prompt: prompts.append(prompt),
            output=output.append,
        )

        self.assertEqual(len(prompts), 1)
        rendered = "\n".join(output + prompts)
        self.assertIn("STAGE 3/10 ACCEPTED", rendered)
        self.assertIn("LIFT YOUR FINGER COMPLETELY", rendered)
        self.assertIn("PLACE THE SAME FINGER AGAIN", rendered)

    def test_finger_name_is_required_and_validated(self):
        module = load_script()
        args = module.parse_args(["-f", "left-thumb", "alice"])
        self.assertEqual(args.finger, "left-thumb")
        self.assertEqual(args.username, "alice")


if __name__ == "__main__":
    unittest.main()
