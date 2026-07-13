import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from egis_driver.fingerprint_matcher import FingerprintMatcher, THRESHOLD_KEYS
from egis_driver.matcher_config import MatcherConfig
from egis_driver.persistence import Persistence


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root, patterns=("*.py",)):
    root = Path(root)
    digest = hashlib.sha256()
    files = sorted(
        path for pattern in patterns for path in root.rglob(pattern)
        if path.is_file()
    )
    for path in files:
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def load_lab_config(path):
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    matcher = MatcherConfig.from_dict(data.get("matcher"))
    thresholds = data.get("thresholds", {})
    missing = sorted(set(THRESHOLD_KEYS) - set(thresholds))
    if missing:
        raise ValueError(f"missing threshold keys: {', '.join(missing)}")
    acceptance = data.get("acceptance", {})
    normalized_acceptance = {
        "min_genuine_pass_rate": float(acceptance.get("min_genuine_pass_rate", 0.75)),
        "max_impostor_accepts": int(acceptance.get("max_impostor_accepts", 0)),
        "max_p95_ms": float(acceptance.get("max_p95_ms", 250.0)),
        "max_latency_regression": float(acceptance.get("max_latency_regression", 0.20)),
    }
    frames_per_attempt = int(data.get("frames_per_attempt", 3))
    if frames_per_attempt < 1:
        raise ValueError("frames_per_attempt must be positive")
    min_confirmed_attempts = int(data.get("min_confirmed_attempts", 1))
    if min_confirmed_attempts < 1:
        raise ValueError("min_confirmed_attempts must be positive")
    return {
        "name": str(data.get("name") or Path(path).stem),
        "matcher": matcher,
        "thresholds": thresholds,
        "acceptance": normalized_acceptance,
        "frames_per_attempt": frames_per_attempt,
        "min_confirmed_attempts": min_confirmed_attempts,
    }


def passes_thresholds(metrics, thresholds):
    if not metrics:
        return False
    return all((
        metrics.get("inliers", 0) >= thresholds["min_inliers"],
        metrics.get("inlier_ratio", 0.0) >= thresholds["min_inlier_ratio"],
        metrics.get("inlier_frames", 0) >= thresholds["min_inlier_frames"],
        metrics.get("max_frame_inliers", 0) >= thresholds["min_frame_inliers"],
        metrics.get("margin", 0.0) >= thresholds["min_margin"],
        metrics.get("ncc", 0.0) >= thresholds["min_ncc"],
        metrics.get("orientation", 0.0) >= thresholds["min_orientation"],
        metrics.get("ridge_score", 0.0) >= thresholds["min_ridge_score"],
    ))


def percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return float(ordered[index])


def _run_once(dataset_root, config):
    persistence = Persistence(str(dataset_root))
    matcher = FingerprintMatcher(
        persistence=persistence,
        matcher_config=config["matcher"],
    )
    records = []
    for base, meta, frames in persistence.list_samples():
        started = time.perf_counter()
        attempt_stats = []
        attempt_elapsed_ms = []
        consecutive_accepts = 0
        confirmed_stats = None
        window = config["frames_per_attempt"]
        for offset in range(0, len(frames), window):
            attempt_frames = frames[offset:offset + window]
            if len(attempt_frames) < window:
                continue
            attempt_started = time.perf_counter()
            matcher.verify_finger_multiframe(
                attempt_frames,
                username=meta["username"],
                finger_name=meta["target_finger"],
                apply_thresholds=False,
                thresholds_override=config["thresholds"],
            )
            stats = dict(matcher.last_verify_stats)
            attempt_stats.append(stats)
            attempt_elapsed_ms.append(
                (time.perf_counter() - attempt_started) * 1000.0
            )
            if passes_thresholds(stats.get("best", {}), config["thresholds"]):
                consecutive_accepts += 1
                if consecutive_accepts >= config["min_confirmed_attempts"]:
                    confirmed_stats = stats
                    break
            else:
                consecutive_accepts = 0
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if not attempt_stats:
            attempt_stats = [{"best": {}, "reject_reason": "incomplete_attempt"}]
        stats = confirmed_stats or max(
            attempt_stats,
            key=lambda item: item.get("best", {}).get("score", 0.0),
        )
        best = stats.get("best", {})
        accepted = confirmed_stats is not None
        records.append({
            "sample": base,
            "username": meta["username"],
            "target_finger": meta["target_finger"],
            "actual_finger": meta["actual_finger"],
            "label": meta["label"],
            "accepted": accepted,
            "reject_reason": None if accepted else stats.get("reject_reason"),
            "elapsed_ms": elapsed_ms,
            "attempt_elapsed_ms": attempt_elapsed_ms,
            "attempts": len(attempt_stats),
            "accepted_attempt": (
                attempt_stats.index(stats) + 1 if accepted else None
            ),
            "keypoints": stats.get("keypoints", 0),
            "good_matches": stats.get("good_matches", 0),
            "candidates": stats.get("candidates", 0),
            "best": best,
        })
    return records


def _decision_signature(records):
    decisions = [
        {
            "sample": record["sample"],
            "accepted": record["accepted"],
            "identity": record["best"].get("name"),
            "reject_reason": record["reject_reason"],
        }
        for record in records
    ]
    encoded = json.dumps(decisions, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _summarize(records, acceptance):
    by_target = {}
    for record in records:
        key = f"{record['username']}/{record['target_finger']}"
        target = by_target.setdefault(key, {
            "genuine_total": 0,
            "genuine_scored": 0,
            "genuine_pass": 0,
            "impostor_total": 0,
            "impostor_scored": 0,
            "impostor_accept": 0,
        })
        scored = bool(record["best"])
        if record["label"] == "genuine":
            target["genuine_total"] += 1
            target["genuine_scored"] += int(scored)
            target["genuine_pass"] += int(record["accepted"])
        else:
            target["impostor_total"] += 1
            target["impostor_scored"] += int(scored)
            target["impostor_accept"] += int(record["accepted"])

    target_gates = {}
    for key, target in by_target.items():
        required = math.ceil(
            target["genuine_total"] * acceptance["min_genuine_pass_rate"]
        )
        target["genuine_pass_required"] = required
        target_gates[key] = (
            target["genuine_pass"] >= required and
            target["impostor_accept"] <= acceptance["max_impostor_accepts"]
        )

    latencies = [
        elapsed
        for record in records
        for elapsed in record.get("attempt_elapsed_ms", [record["elapsed_ms"]])
    ]
    latency = {
        "p50_ms": percentile(latencies, 0.50),
        "p95_ms": percentile(latencies, 0.95),
        "max_ms": max(latencies, default=0.0),
    }
    return by_target, target_gates, latency


def evaluate(dataset_root, config_path, source_root, repeats=2):
    dataset_root = Path(dataset_root).resolve()
    config = load_lab_config(config_path)
    runs = [_run_once(dataset_root, config) for _ in range(repeats)]
    signatures = [_decision_signature(records) for records in runs]
    deterministic = len(set(signatures)) == 1
    records = runs[0]
    by_target, target_gates, latency = _summarize(records, config["acceptance"])

    manifest_path = dataset_root / "dataset-manifest.json"
    manifest_hash = sha256_file(manifest_path) if manifest_path.exists() else None
    manifest = {}
    if manifest_path.exists():
        with open(manifest_path, encoding="utf-8") as stream:
            manifest = json.load(stream)
    latency_ok = latency["p95_ms"] <= config["acceptance"]["max_p95_ms"]
    passed = deterministic and latency_ok and all(target_gates.values()) and bool(target_gates)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "name": config["name"],
            "matcher": config["matcher"].to_dict(),
            "thresholds": config["thresholds"],
            "acceptance": config["acceptance"],
        },
        "dataset": {
            "root": str(dataset_root),
            "manifest_sha256": manifest_hash,
            "role": manifest.get("role", "unknown"),
        },
        "source": {
            "root": str(Path(source_root).resolve()),
            "python_tree_sha256": tree_digest(
                Path(source_root) / "open-fprintd-eh575",
            ),
        },
        "repeats": repeats,
        "decision_sha256": signatures[0],
        "deterministic": deterministic,
        "targets": by_target,
        "latency": latency,
        "gates": {
            "targets": target_gates,
            "latency": latency_ok,
            "deterministic": deterministic,
            "passed": passed,
        },
        "records": records,
    }


def write_report(report, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{stamp}-{report['config']['name']}"
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    with open(json_path, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)

    lines = [
        f"# Egis Lab: {report['config']['name']}",
        "",
        f"- Passed: `{report['gates']['passed']}`",
        f"- Deterministic: `{report['deterministic']}`",
        f"- Decision: `{report['decision_sha256']}`",
        f"- Latency p50/p95: `{report['latency']['p50_ms']:.1f}` / "
        f"`{report['latency']['p95_ms']:.1f} ms`",
        "",
        "| Target | Genuine | Required | Impostor accepts | Gate |",
        "|---|---:|---:|---:|---|",
    ]
    for target, values in sorted(report["targets"].items()):
        lines.append(
            f"| {target} | {values['genuine_pass']}/{values['genuine_total']} | "
            f"{values['genuine_pass_required']} | {values['impostor_accept']} | "
            f"{'PASS' if report['gates']['targets'][target] else 'FAIL'} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path
