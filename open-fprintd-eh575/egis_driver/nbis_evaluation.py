"""Private leave-one-touch-out evaluation for the NBIS candidate engine."""

from collections import defaultdict
import statistics
import time

from egis_driver.sequence_evaluation import read_touch
from egis_driver.streaming import FrameStatus
from egis_matcher.nbis import NbisEngine
from egis_matcher.stitching import TouchStitcher


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile / 100.0)
    return float(ordered[index])


def stitch_sequence(directory, blend="weighted", preprocessor=None):
    manifest, messages, spec = read_touch(directory)
    stitcher = TouchStitcher(spec, blend=blend)
    stitcher.begin_touch()
    started = time.perf_counter()
    for message in messages:
        if message.dropped_before:
            stitcher.discontinuity()
        if message.status is FrameStatus.VALID:
            pixels = (preprocessor.correct(message.pixels).tobytes()
                      if preprocessor is not None else message.pixels)
            stitcher.observe(pixels, message.sequence)
        elif message.status is FrameStatus.IO_ERROR:
            stitcher.discontinuity()
    stitched = stitcher.snapshot(force=True)
    return manifest, stitched, (time.perf_counter() - started) * 1000.0


def evaluate_nbis(sequences, bin_dir=None, preprocessor=None):
    prepared_by_blend = {}
    for blend in ("weighted", "winner", "median"):
        prepared = []
        for directory in sequences:
            manifest, stitched, stitching_ms = stitch_sequence(
                directory, blend, preprocessor=preprocessor)
            metadata = manifest.get("metadata", {})
            prepared.append({
                "finger": metadata.get("finger"),
                "role": metadata.get("role"),
                "label": metadata.get("label"),
                "stitched": stitched,
                "stitching_ms": stitching_ms,
            })
        prepared_by_blend[blend] = prepared

    configurations = [
        {"scale": scale, "invert": invert, "enhance": enhance, "blend": blend}
        for blend in ("weighted", "winner", "median")
        for scale in (0.75, 1.0, 1.25, 1.5, 2.0)
        for invert in (False, True)
        for enhance in (False, True)
    ]
    results = []
    for config in configurations:
        prepared = prepared_by_blend[config["blend"]]
        engine_config = {key: value for key, value in config.items() if key != "blend"}
        engine = NbisEngine(bin_dir=bin_dir, **engine_config)
        extracted = []
        for item in prepared:
            result = (engine.extract(item["stitched"])
                      if item["stitched"] is not None else None)
            extracted.append(result)
        comparisons = []
        genuine_scores = []
        impostor_scores = []
        comparison_times = []
        genuine_margins = []
        correct = 0
        genuine_total = 0
        enrollment = defaultdict(list)
        for index, item in enumerate(prepared):
            if item["role"] == "enrollment" and extracted[index] and extracted[index].template:
                enrollment[item["finger"]].append(index)
        for probe_index, item in enumerate(prepared):
            result = extracted[probe_index]
            if result is None or result.template is None:
                continue
            is_enrollment = item["role"] == "enrollment"
            identity_scores = {}
            for identity, template_indices in enrollment.items():
                eligible = [index for index in template_indices
                            if not (is_enrollment and index == probe_index)]
                scores = []
                for index in eligible:
                    compare_started = time.perf_counter()
                    scores.append(engine.compare(
                        result.template, extracted[index].template))
                    comparison_times.append(
                        (time.perf_counter() - compare_started) * 1000.0)
                if scores:
                    identity_scores[identity] = max(scores)
                    target = genuine_scores if identity == item["finger"] else impostor_scores
                    target.extend(scores)
            if is_enrollment:
                genuine_total += 1
                ranked_identities = sorted(
                    identity_scores.items(), key=lambda pair: pair[1], reverse=True)
                if (ranked_identities and ranked_identities[0][0] == item["finger"] and
                        (len(ranked_identities) == 1 or
                         ranked_identities[0][1] > ranked_identities[1][1])):
                    correct += 1
                    genuine_margins.append(
                        ranked_identities[0][1] -
                        (ranked_identities[1][1] if len(ranked_identities) > 1 else 0))
            comparisons.append({
                "probe": item["label"],
                "finger": item["finger"],
                "scores": identity_scores,
            })
        extraction_times = [result.elapsed_ms for result in extracted if result]
        success_count = sum(bool(result and result.template) for result in extracted)
        minimum_genuine = min(genuine_scores) if genuine_scores else None
        maximum_impostor = max(impostor_scores) if impostor_scores else None
        separated = bool(
            minimum_genuine is not None and maximum_impostor is not None and
            minimum_genuine > maximum_impostor)
        result = {
            "config": {**engine.config(), "blend": config["blend"]},
            "extraction_success": success_count,
            "extraction_total": len(extracted),
            "genuine_correct": correct,
            "genuine_total": genuine_total,
            "minimum_genuine_score": minimum_genuine,
            "maximum_impostor_score": maximum_impostor,
            "score_gap": (minimum_genuine - maximum_impostor
                          if separated else None),
            "minimum_genuine_identity_margin": (
                min(genuine_margins) if genuine_margins else None),
            "separated": separated,
            "p95_extraction_ms": _percentile(extraction_times, 95),
            "p95_comparison_ms": _percentile(comparison_times, 95),
            "mean_stitching_ms": statistics.fmean(
                item["stitching_ms"] for item in prepared),
            "comparisons": comparisons,
            "extractions": [
                {
                    "label": item["label"],
                    "finger": item["finger"],
                    "reason": result.reason if result else "stitch_failed",
                    "minutiae": (result.template.minutiae
                                 if result and result.template else 0),
                    "elapsed_ms": result.elapsed_ms if result else 0.0,
                }
                for item, result in zip(prepared, extracted)
            ],
        }
        result["gate_passed"] = bool(
            separated and genuine_total and correct / genuine_total >= 0.90 and
            any(item["role"] == "development" for item in prepared) and
            success_count / len(extracted) >= 0.90 and
            result["minimum_genuine_identity_margin"] is not None and
            result["minimum_genuine_identity_margin"] > 0 and
            result["p95_extraction_ms"] +
            4 * (result["p95_comparison_ms"] or 0.0) <= 500.0)
        results.append(result)

    ranked = sorted(results, key=lambda item: (
        item["gate_passed"], item["genuine_correct"],
        item["score_gap"] if item["score_gap"] is not None else -1,
        item["extraction_success"], -item["p95_extraction_ms"],
    ), reverse=True)
    return {
        "schema_version": 1,
        "experimental": True,
        "promotable": False,
        "sequence_count": len(next(iter(prepared_by_blend.values()), [])),
        "gate_passed": bool(ranked and ranked[0]["gate_passed"]),
        "calibration_digest": (preprocessor.profile.digest if preprocessor else None),
        "winner": ranked[0] if ranked else None,
        "configurations": results,
    }
