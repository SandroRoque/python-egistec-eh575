"""Private full-touch and progressive-prefix evaluation for SourceAFIS."""

from collections import defaultdict
import statistics
import time

from egis_driver.nbis_evaluation import _percentile, stitch_sequence
from egis_driver.sequence_evaluation import read_touch
from egis_driver.streaming import FrameStatus
from egis_matcher.sourceafis import SourceAfisEngine
from egis_matcher.stitching import TouchStitcher


def _identity_scores(engine, template, enrollment, excluded=None):
    scores = {}
    timings = []
    for identity, candidates in enrollment.items():
        eligible = [candidate for key, candidate in candidates if key != excluded]
        if not eligible:
            continue
        values = []
        for candidate in eligible:
            started = time.perf_counter()
            values.append(engine.compare(template, candidate))
            timings.append((time.perf_counter() - started) * 1000.0)
        scores[identity] = max(values)
    return scores, timings


def _progressive_probe(engine, directory, enrollment, threshold, excluded=None,
                       step=10, preprocessor=None):
    manifest, messages, spec = read_touch(directory)
    stitcher = TouchStitcher(spec, blend="median")
    stitcher.begin_touch()
    points = []
    valid = 0
    for message in messages:
        if message.dropped_before:
            stitcher.discontinuity()
        if message.status is FrameStatus.IO_ERROR:
            stitcher.discontinuity()
            continue
        if message.status is not FrameStatus.VALID:
            continue
        pixels = (preprocessor.correct(message.pixels).tobytes()
                  if preprocessor is not None else message.pixels)
        stitcher.observe(pixels, message.sequence)
        valid += 1
        if valid < step or valid % step:
            continue
        started = time.perf_counter()
        stitched = stitcher.snapshot(force=True)
        extraction = engine.extract(stitched) if stitched is not None else None
        scores, timings = ({}, [])
        if extraction and extraction.template:
            scores, timings = _identity_scores(
                engine, extraction.template, enrollment, excluded=excluded)
        ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
        points.append({
            "valid_frames": valid,
            "identity": ranked[0][0] if ranked else None,
            "score": ranked[0][1] if ranked else None,
            "accepted": bool(ranked and ranked[0][1] > threshold),
            "processing_ms": (time.perf_counter() - started) * 1000.0,
            "comparison_ms": sum(timings),
        })
    expected = manifest.get("metadata", {}).get("finger")
    stable = None
    for index, point in enumerate(points):
        suffix = points[index:]
        if (len(suffix) >= 2 and point["accepted"] and
                all(item["identity"] == expected and item["accepted"] for item in suffix)):
            stable = point
            break
    return {
        "label": manifest.get("metadata", {}).get("label"),
        "finger": expected,
        "role": manifest.get("metadata", {}).get("role"),
        "stable_correct_frames": stable["valid_frames"] if stable else None,
        "stable_processing_ms": stable["processing_ms"] if stable else None,
        "points": points,
    }


def evaluate_sourceafis(sequences, home=None, preprocessor=None):
    prepared_by_blend = {}
    for blend in ("weighted", "winner", "median"):
        prepared = []
        for directory in sequences:
            manifest, stitched, stitching_ms = stitch_sequence(
                directory, blend, preprocessor=preprocessor)
            metadata = manifest.get("metadata", {})
            prepared.append({
                "directory": directory, "finger": metadata.get("finger"),
                "role": metadata.get("role"), "label": metadata.get("label"),
                "stitched": stitched, "stitching_ms": stitching_ms,
            })
        prepared_by_blend[blend] = prepared
    configurations = [
        {"blend": blend, "scale": scale, "invert": invert}
        for blend in ("weighted", "winner", "median")
        for scale in (1.0, 1.5, 2.0)
        for invert in (False, True)
    ]
    results = []
    for config in configurations:
        prepared = prepared_by_blend[config["blend"]]
        with SourceAfisEngine(home=home, scale=config["scale"], invert=config["invert"]) as engine:
            extracted = [engine.extract(item["stitched"]) if item["stitched"] is not None else None
                         for item in prepared]
            enrollment = defaultdict(list)
            for index, (item, result) in enumerate(zip(prepared, extracted)):
                if item["role"] == "enrollment" and result and result.template:
                    enrollment[item["finger"]].append((index, result.template))
            genuine_scores, impostor_scores, comparison_times, margins = [], [], [], []
            comparisons, correct, genuine_total = [], 0, 0
            for index, (item, result) in enumerate(zip(prepared, extracted)):
                if not result or not result.template:
                    continue
                scores, timings = _identity_scores(
                    engine, result.template, enrollment,
                    excluded=index if item["role"] == "enrollment" else None)
                comparison_times.extend(timings)
                for identity, score in scores.items():
                    (genuine_scores if identity == item["finger"] else impostor_scores).append(score)
                ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
                if item["role"] == "enrollment":
                    genuine_total += 1
                    if (ranked and ranked[0][0] == item["finger"] and
                            (len(ranked) == 1 or ranked[0][1] > ranked[1][1])):
                        correct += 1
                        margins.append(ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0))
                comparisons.append({"probe": item["label"], "finger": item["finger"], "scores": scores})
            minimum_genuine = min(genuine_scores) if genuine_scores else None
            maximum_impostor = max(impostor_scores) if impostor_scores else None
            separated = bool(minimum_genuine is not None and maximum_impostor is not None and
                             minimum_genuine > maximum_impostor)
            extraction_times = [value.elapsed_ms for value in extracted if value]
            success = sum(bool(value and value.template) for value in extracted)
            result = {
                "config": {**engine.config(), "blend": config["blend"]},
                "extraction_success": success, "extraction_total": len(extracted),
                "genuine_correct": correct, "genuine_total": genuine_total,
                "minimum_genuine_score": minimum_genuine,
                "maximum_impostor_score": maximum_impostor,
                "score_gap": minimum_genuine - maximum_impostor if separated else None,
                "minimum_genuine_identity_margin": min(margins) if margins else None,
                "separated": separated,
                "p95_extraction_ms": _percentile(extraction_times, 95),
                "p95_comparison_ms": _percentile(comparison_times, 95),
                "mean_stitching_ms": statistics.fmean(item["stitching_ms"] for item in prepared),
                "comparisons": comparisons,
                "extractions": [{"label": item["label"], "finger": item["finger"],
                                 "reason": value.reason if value else "stitch_failed",
                                 "elapsed_ms": value.elapsed_ms if value else 0.0}
                                for item, value in zip(prepared, extracted)],
            }
            result["gate_passed"] = bool(
                separated and genuine_total and correct / genuine_total >= .90 and
                any(item["role"] == "development" for item in prepared) and
                success / len(extracted) >= .90 and result["minimum_genuine_identity_margin"] and
                result["p95_extraction_ms"] + 4 * (result["p95_comparison_ms"] or 0) <= 500)
            results.append(result)
    ranked = sorted(results, key=lambda item: (
        item["gate_passed"], item["genuine_correct"],
        item["score_gap"] if item["score_gap"] is not None else -1,
        item["extraction_success"], -item["p95_extraction_ms"]), reverse=True)
    winner = ranked[0] if ranked else None
    progressive = []
    if winner:
        config = winner["config"]
        prepared = prepared_by_blend[config["blend"]]
        with SourceAfisEngine(home=home, scale=config["scale"], invert=config["invert"]) as engine:
            enrollment = defaultdict(list)
            for index, item in enumerate(prepared):
                if item["role"] != "enrollment" or item["stitched"] is None:
                    continue
                extraction = engine.extract(item["stitched"])
                if extraction.template:
                    enrollment[item["finger"]].append((index, extraction.template))
            threshold = float(winner["maximum_impostor_score"] or 0)
            progressive = [_progressive_probe(
                engine, item["directory"], enrollment, threshold,
                excluded=index if item["role"] == "enrollment" else None,
                preprocessor=preprocessor)
                for index, item in enumerate(prepared)]
    genuine_prefixes = [item for item in progressive if item["role"] == "enrollment"]
    impostor_prefixes = [item for item in progressive if item["role"] == "development"]
    prefix_passed = bool(
        genuine_prefixes and impostor_prefixes and
        sum(item["stable_correct_frames"] is not None for item in genuine_prefixes) /
        len(genuine_prefixes) >= .90 and
        all(not any(point["accepted"] for point in item["points"])
            for item in impostor_prefixes))
    return {
        "schema_version": 1, "experimental": True, "promotable": False,
        "sequence_count": len(next(iter(prepared_by_blend.values()), [])),
        "full_touch_gate_passed": bool(winner and winner["gate_passed"]),
        "prefix_gate_passed": prefix_passed,
        "gate_passed": bool(winner and winner["gate_passed"] and prefix_passed),
        "calibration_digest": (preprocessor.profile.digest if preprocessor else None),
        "winner": winner, "progressive": progressive, "configurations": results,
    }
