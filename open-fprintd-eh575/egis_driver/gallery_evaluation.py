"""Automated private replay for presentation-gallery feature engines."""

import time

import numpy as np

from egis_driver.sequence_evaluation import read_touch
from egis_driver.streaming import FrameStatus
from egis_matcher.gallery import GalleryBuilder, StreamingGalleryMatcher


def _percentile(values, percentile):
    return float(np.percentile(values, percentile)) if values else None


def _longest_run(points, identity, score_threshold, margin_threshold):
    best = current = 0
    for point in points:
        qualifies = (
            point["identity"] == identity and
            point["score"] >= score_threshold and
            point["margin"] >= margin_threshold)
        current = current + 1 if qualifies else 0
        best = max(best, current)
    return best


def evaluate_feature_gallery(sequence_paths, engine, frame_spec):
    prepared = []
    enrollment = {}
    for path in sequence_paths:
        manifest, messages, spec = read_touch(path)
        if spec != frame_spec:
            raise ValueError("gallery evaluation sequences change geometry")
        metadata = manifest.get("metadata", {})
        frames = [item.pixels for item in messages
                  if item.status is FrameStatus.VALID]
        item = {
            "path": str(path), "label": metadata.get("label"),
            "finger": metadata.get("finger"), "role": metadata.get("role"),
            "frames": frames,
        }
        prepared.append(item)
        if item["role"] == "enrollment":
            enrollment.setdefault(item["finger"], []).append(frames)
    if not enrollment:
        raise ValueError("gallery evaluation requires enrollment sequences")
    galleries = {}
    build_started = time.perf_counter()
    for identity, presentations in enrollment.items():
        gallery = GalleryBuilder(engine, frame_spec).build(identity, presentations)
        if gallery.entries:
            galleries[identity] = gallery
    if not galleries:
        raise ValueError("feature extraction produced no enrollment galleries")
    build_ms = (time.perf_counter() - build_started) * 1000.0
    trials = []
    extraction_times = []
    comparison_times = []
    for item in prepared:
        if item["role"] == "enrollment":
            continue
        matcher = StreamingGalleryMatcher(engine, galleries, frame_spec)
        matcher.begin_touch()
        points = []
        started = time.perf_counter()
        for sequence, frame in enumerate(item["frames"], 1):
            decision = matcher.observe(frame, sequence)
            extraction_times.append(decision.metrics["extraction_ms"])
            comparison_times.append(decision.metrics["comparison_ms"])
            points.append({
                "sequence": sequence,
                "identity": decision.metrics["best_identity"],
                "score": decision.score,
                "margin": decision.margin,
                "scores": decision.metrics["scores"],
                "reason": decision.reason,
            })
        expected = item["finger"] if item["finger"] in galleries else None
        trials.append({
            "label": item["label"], "finger": item["finger"],
            "expected_identity": expected,
            "expected": "genuine" if expected else "impostor",
            "frames": len(item["frames"]),
            "extraction_failures": matcher.extraction_failures,
            "processing_ms": (time.perf_counter() - started) * 1000.0,
            "points": points,
        })
    genuine = [trial for trial in trials if trial["expected"] == "genuine"]
    impostor = [trial for trial in trials if trial["expected"] == "impostor"]
    scores = sorted({point["score"] for trial in trials for point in trial["points"]})
    if len(scores) > 100:
        scores = sorted(set(float(value) for value in np.percentile(scores, np.arange(101))))
    policies = []
    for score_threshold in scores or [0.0]:
        for margin_threshold in (0.0, 5.0, 10.0):
            for required_run in (1, 2, 3, 5):
                genuine_accepts = sum(
                    _longest_run(trial["points"], trial["expected_identity"],
                                 score_threshold, margin_threshold) >= required_run
                    for trial in genuine)
                impostor_accepts = sum(
                    max((_longest_run(trial["points"], identity,
                                      score_threshold, margin_threshold)
                         for identity in galleries), default=0) >= required_run
                    for trial in impostor)
                policies.append({
                    "score_threshold": score_threshold,
                    "margin_threshold": margin_threshold,
                    "required_consecutive_frames": required_run,
                    "genuine_accepts": genuine_accepts,
                    "genuine_total": len(genuine),
                    "impostor_accepts": impostor_accepts,
                    "impostor_total": len(impostor),
                })
    policies.sort(key=lambda item: (
        item["impostor_accepts"] == 0, item["genuine_accepts"],
        -item["required_consecutive_frames"], item["score_threshold"]),
        reverse=True)
    winner = policies[0] if policies else None
    if winner:
        for trial in trials:
            target = trial["expected_identity"]
            if target is None:
                trial["time_to_evidence_frame"] = None
                continue
            current = 0
            reached = None
            for point in trial["points"]:
                if (point["identity"] == target and
                        point["score"] >= winner["score_threshold"] and
                        point["margin"] >= winner["margin_threshold"]):
                    current += 1
                    if current >= winner["required_consecutive_frames"]:
                        reached = point["sequence"]
                        break
                else:
                    current = 0
            trial["time_to_evidence_frame"] = reached
    total_frames = sum(trial["frames"] for trial in trials)
    failures = sum(trial["extraction_failures"] for trial in trials)
    extraction_rate = ((total_frames - failures) / total_frames
                       if total_frames else 0.0)
    gate = bool(
        winner and genuine and impostor and
        winner["genuine_accepts"] == len(genuine) and
        winner["impostor_accepts"] == 0 and extraction_rate >= .90 and
        (_percentile(extraction_times, 95) or float("inf")) <= 500)
    return {
        "schema_version": 1,
        "experimental": True,
        "promotable": False,
        "engine": engine.config(),
        "gallery_build_ms": build_ms,
        "galleries": {
            identity: {
                "presentations": gallery.presentations,
                "records": len(gallery.entries),
                "representations": {
                    kind: sum(entry.representation == kind for entry in gallery.entries)
                    for kind in ("frame", "component")
                },
                "extraction_failures": gallery.extraction_failures,
            } for identity, gallery in galleries.items()
        },
        "trial_count": len(trials),
        "genuine_trials": len(genuine),
        "impostor_trials": len(impostor),
        "probe_extraction_rate": extraction_rate,
        "p50_extraction_ms": _percentile(extraction_times, 50),
        "p95_extraction_ms": _percentile(extraction_times, 95),
        "p50_comparison_ms": _percentile(comparison_times, 50),
        "p95_comparison_ms": _percentile(comparison_times, 95),
        "winner": winner,
        "gate_passed": gate,
        "trials": trials,
        "policies": policies,
    }
