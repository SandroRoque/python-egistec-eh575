import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import numpy as np

from egis_driver.evaluation import (
    _decision_signature,
    _summarize,
    evaluate,
    tree_digest,
    write_report,
)
from egis_driver.lab_artifact import build_candidate, extract_and_validate
from egis_driver.lab_artifact import compare_reports
from egis_driver.matcher_config import MatcherConfig
from egis_driver.persistence import Persistence


ROOT = Path(__file__).resolve().parents[1]


class MatcherConfigTests(unittest.TestCase):
    def test_defaults_preserve_established_matcher_behavior(self):
        config = MatcherConfig()

        self.assertEqual(config.sift_ratio, 0.75)
        self.assertEqual(config.index_scope, "username")
        self.assertEqual(config.index_backend, "bf")
        self.assertEqual(config.random_seed, 0)
        self.assertEqual(config.proposal_scope, "template")
        self.assertEqual(config.max_candidates, 12)
        self.assertEqual(config.ransac_reproj_threshold, 8.0)

    def test_unknown_or_invalid_values_are_rejected(self):
        with self.assertRaises(ValueError):
            MatcherConfig.from_dict({"unknown": 1})
        with self.assertRaises(ValueError):
            MatcherConfig.from_dict({"sift_ratio": 1.0})
        with self.assertRaises(ValueError):
            MatcherConfig.from_dict({"index_scope": "finger"})
        with self.assertRaises(ValueError):
            MatcherConfig.from_dict({"index_backend": "approximate"})
        with self.assertRaises(ValueError):
            MatcherConfig.from_dict({"proposal_scope": "user"})


class EvaluationSummaryTests(unittest.TestCase):
    def _record(self, sample, label, accepted, best=True, elapsed=10.0):
        return {
            "sample": sample,
            "username": "testuser",
            "target_finger": "right-index-finger",
            "actual_finger": "right-index-finger" if label == "genuine" else "right-thumb",
            "label": label,
            "accepted": accepted,
            "reject_reason": None if accepted else "threshold_mismatch",
            "elapsed_ms": elapsed,
            "best": {"name": "testuser_right-index-finger"} if best else {},
        }

    def test_unscored_genuine_counts_against_gate(self):
        records = [
            self._record(f"genuine-{index}", "genuine", index < 5, best=index < 5)
            for index in range(8)
        ] + [
            self._record(f"impostor-{index}", "impostor", False, best=False)
            for index in range(8)
        ]
        acceptance = {
            "min_genuine_pass_rate": 0.75,
            "max_impostor_accepts": 0,
            "max_p95_ms": 250.0,
        }

        targets, gates, latency = _summarize(records, acceptance)

        target = targets["testuser/right-index-finger"]
        self.assertEqual(target["genuine_pass_required"], 6)
        self.assertEqual(target["genuine_pass"], 5)
        self.assertFalse(gates["testuser/right-index-finger"])
        self.assertEqual(latency["p95_ms"], 10.0)

    def test_decision_digest_excludes_latency(self):
        first = [self._record("sample", "genuine", True, elapsed=10.0)]
        second = [self._record("sample", "genuine", True, elapsed=99.0)]

        self.assertEqual(_decision_signature(first), _decision_signature(second))

    def test_end_to_end_evaluation_uses_offline_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            persistence = Persistence(str(tmp))
            persistence.ensure_dirs()
            persistence.save_sample(
                "sample",
                {
                    f"frame_{index}": np.zeros(52 * 103, dtype=np.uint8)
                    for index in range(6)
                },
                {
                    "username": "testuser",
                    "target_finger": "right-index-finger",
                    "actual_finger": "right-index-finger",
                    "label": "genuine",
                    "num_frames": 6,
                },
            )
            (tmp / "dataset-manifest.json").write_text(json.dumps({
                "schema_version": 1,
                "role": "development",
                "files": {},
            }))

            report = evaluate(
                tmp,
                ROOT / "lab-configs" / "baseline.json",
                ROOT,
                repeats=2,
            )
            json_path, markdown_path = write_report(report, tmp / "results")

            self.assertFalse(report["gates"]["passed"])
            self.assertTrue(report["deterministic"])
            self.assertEqual(report["dataset"]["role"], "development")
            self.assertEqual(report["records"][0]["attempts"], 2)
            self.assertTrue(json_path.is_file())
            self.assertTrue(markdown_path.is_file())


class CandidateArtifactTests(unittest.TestCase):
    def _report(self, p95, source_digest, dataset="dataset-hash", role="holdout"):
        return {
            "dataset": {"manifest_sha256": dataset, "role": role},
            "source": {"python_tree_sha256": source_digest},
            "decision_sha256": "decision",
            "latency": {"p95_ms": p95},
            "gates": {"passed": True},
            "config": {"acceptance": {"max_latency_regression": 0.20}},
        }

    def test_build_and_validate_candidate(self):
        digest = tree_digest(ROOT / "open-fprintd-eh575")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            baseline = tmp / "baseline.json"
            candidate = tmp / "candidate.json"
            baseline.write_text(json.dumps(self._report(100.0, digest)))
            candidate.write_text(json.dumps(self._report(110.0, digest)))

            artifact = build_candidate(ROOT, candidate, baseline, tmp / "dist")
            with tempfile.TemporaryDirectory() as extracted:
                root, manifest = extract_and_validate(artifact, extracted)

            self.assertTrue(manifest["acceptance"]["passed"])
            self.assertIn("payload/egis-bridge", manifest["files"])
            self.assertFalse(any("__pycache__" in name for name in manifest["files"]))
            dry_run = subprocess.run(
                [sys.executable, str(ROOT / "promote-candidate"), "--dry-run", str(artifact)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)

    def test_development_report_cannot_be_packaged(self):
        digest = tree_digest(ROOT / "open-fprintd-eh575")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            baseline = tmp / "baseline.json"
            candidate = tmp / "candidate.json"
            baseline.write_text(json.dumps(self._report(100.0, digest, role="development")))
            candidate.write_text(json.dumps(self._report(100.0, digest, role="development")))

            with self.assertRaisesRegex(ValueError, "holdout"):
                build_candidate(ROOT, candidate, baseline, tmp / "dist")

    def test_failed_baseline_is_not_a_relative_latency_reference(self):
        baseline = self._report(20.0, "source")
        baseline["gates"]["passed"] = False
        candidate = self._report(100.0, "source")

        comparison = compare_reports(baseline, candidate)

        self.assertFalse(comparison["baseline_eligible"])
        self.assertTrue(comparison["relative_latency_ok"])
        self.assertTrue(comparison["passed"])

    def test_tampered_candidate_is_rejected(self):
        digest = tree_digest(ROOT / "open-fprintd-eh575")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            baseline = tmp / "baseline.json"
            candidate = tmp / "candidate.json"
            baseline.write_text(json.dumps(self._report(100.0, digest)))
            candidate.write_text(json.dumps(self._report(100.0, digest)))
            artifact = build_candidate(ROOT, candidate, baseline, tmp / "dist")

            unpacked = tmp / "unpacked"
            with tarfile.open(artifact, "r:gz") as archive:
                archive.extractall(unpacked, filter="data")
            root = next(unpacked.iterdir())
            (root / "payload" / "egis-bridge").write_text("tampered")
            tampered = tmp / "tampered.tar.gz"
            with tarfile.open(tampered, "w:gz") as archive:
                archive.add(root, arcname=root.name)

            with tempfile.TemporaryDirectory() as extracted:
                with self.assertRaisesRegex(ValueError, "mismatch"):
                    extract_and_validate(tampered, extracted)


class LabCliTests(unittest.TestCase):
    def test_help_runs_without_privileges(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "egis-lab"), "--help"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("snapshot", result.stdout)
        self.assertIn("evaluate", result.stdout)


if __name__ == "__main__":
    unittest.main()
