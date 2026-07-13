import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from egis_driver.evaluation import sha256_file, tree_digest


PAYLOAD_EXECUTABLES = ("open-fprintd", "egis-bridge", "egis-calibrate")


def _load_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def compare_reports(baseline, candidate):
    if baseline["dataset"]["manifest_sha256"] != candidate["dataset"]["manifest_sha256"]:
        raise ValueError("baseline and candidate use different datasets")
    baseline_p95 = float(baseline["latency"]["p95_ms"])
    candidate_p95 = float(candidate["latency"]["p95_ms"])
    regression = (candidate_p95 / baseline_p95 - 1.0) if baseline_p95 else 0.0
    limit = float(candidate["config"]["acceptance"]["max_latency_regression"])
    baseline_eligible = bool(baseline.get("gates", {}).get("passed"))
    relative_latency_ok = not baseline_eligible or regression <= limit
    return {
        "baseline_p95_ms": baseline_p95,
        "candidate_p95_ms": candidate_p95,
        "latency_regression": regression,
        "latency_regression_limit": limit,
        "baseline_eligible": baseline_eligible,
        "relative_latency_ok": relative_latency_ok,
        "passed": bool(candidate["gates"]["passed"] and relative_latency_ok),
    }


def _copy_payload(source_root, payload):
    project = Path(source_root) / "open-fprintd-eh575"
    payload.mkdir(parents=True)
    for executable in PAYLOAD_EXECUTABLES:
        shutil.copy2(project / "bin" / executable, payload / executable)
        (payload / executable).chmod(0o755)
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(project / "egis_driver", payload / "egis_driver", ignore=ignored)
    shutil.copytree(project / "openfprintd", payload / "openfprintd", ignore=ignored)


def _file_manifest(root):
    return {
        str(path.relative_to(root)): {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }


def build_candidate(source_root, candidate_report_path, baseline_report_path, output_dir):
    source_root = Path(source_root).resolve()
    candidate = _load_json(candidate_report_path)
    baseline = _load_json(baseline_report_path)
    comparison = compare_reports(baseline, candidate)
    if not comparison["passed"]:
        raise ValueError("candidate does not pass acceptance and latency gates")
    if candidate["dataset"].get("role") != "holdout":
        raise ValueError("candidate report must use a holdout dataset")
    source_digest = tree_digest(source_root / "open-fprintd-eh575")
    if candidate["source"]["python_tree_sha256"] != source_digest:
        raise ValueError("source changed after the candidate evaluation")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"egis-candidate-{stamp}-{source_digest[:12]}"
    artifact = output_dir / f"{name}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="egis-candidate-") as temp:
        root = Path(temp) / name
        payload = root / "payload"
        reports = root / "reports"
        reports.mkdir(parents=True)
        _copy_payload(source_root, payload)
        shutil.copy2(candidate_report_path, reports / "candidate.json")
        shutil.copy2(baseline_report_path, reports / "baseline.json")
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "name": name,
            "source_python_tree_sha256": source_digest,
            "dataset_manifest_sha256": candidate["dataset"]["manifest_sha256"],
            "decision_sha256": candidate["decision_sha256"],
            "acceptance": comparison,
            "files": _file_manifest(root),
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with tarfile.open(artifact, "w:gz") as archive:
            archive.add(root, arcname=name)
    return artifact


def _safe_extract(archive, destination):
    members = archive.getmembers()
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
            raise ValueError(f"unsafe artifact member: {member.name}")
    archive.extractall(destination, members=members, filter="data")


def extract_and_validate(artifact, destination):
    destination = Path(destination)
    with tarfile.open(artifact, "r:gz") as archive:
        _safe_extract(archive, destination)
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise ValueError("artifact must contain exactly one root directory")
    root = roots[0]
    manifest = _load_json(root / "manifest.json")
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported artifact schema")
    if manifest.get("acceptance", {}).get("passed") is not True:
        raise ValueError("artifact acceptance gate is not satisfied")
    for relative, expected in manifest.get("files", {}).items():
        path = root / relative
        if not path.is_file():
            raise ValueError(f"artifact file is missing: {relative}")
        if path.stat().st_size != expected["size"]:
            raise ValueError(f"artifact size mismatch: {relative}")
        if sha256_file(path) != expected["sha256"]:
            raise ValueError(f"artifact checksum mismatch: {relative}")
    expected_files = set(manifest["files"])
    actual_files = {
        str(path.relative_to(root)) for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if actual_files != expected_files:
        raise ValueError("artifact contains unmanifested files")
    return root, manifest
