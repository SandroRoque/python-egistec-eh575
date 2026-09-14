"""Offline adapters from private sensor observations to experimental atlases."""

import hashlib
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

from egis_driver.sequence_recording import load_sequence
from egis_driver.atlas_storage import implementation_fingerprint
from egis_driver.streaming import FrameStatus
from egis_matcher.atlas import FeatureAtlas, StreamingAtlasMatcher, SEQUENCE_MATCHER_VERSION
from egis_matcher.sequence import TouchTracker


def sequence_source(directory):
    directory = Path(directory).resolve()
    return {
        "path": str(directory),
        "manifest_sha256": hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest(),
        "frames_sha256": hashlib.sha256((directory / "frames.bin").read_bytes()).hexdigest(),
    }


def read_touch(directory, require_complete=False):
    manifest, messages = load_sequence(directory)
    if not messages or not any(item.status is FrameStatus.VALID for item in messages):
        raise ValueError("sequence has no valid frames")
    spec = messages[0].frame_spec
    if any(item.frame_spec != spec for item in messages):
        raise ValueError("sequence changes frame geometry")
    if require_complete and not complete_touch(manifest, messages):
        raise ValueError("enrollment requires a complete sequence with a contact-end event")
    return manifest, messages, spec


def complete_touch(manifest, messages):
    return bool(
        messages and messages[0].sequence == 1 and manifest.get("complete") and
        not manifest.get("recorder_dropped", 0) and
        messages[-1].status is FrameStatus.CONTACT_END and
        not any(item.dropped_before for item in messages) and
        all(right.sequence == left.sequence + 1 for left, right in zip(messages, messages[1:])))


def sequence_events(messages):
    """Signal boundaries before frames; never join across loss or contact changes."""
    previous = None
    for message in messages:
        boundary = (message.dropped_before > 0 or
                    message.status is not FrameStatus.VALID)
        if previous is not None:
            boundary = boundary or (
                message.sequence != previous.sequence + 1 or
                message.generation != previous.generation or
                message.capture_epoch != previous.capture_epoch)
        yield message, boundary
        previous = message


def enroll_sequences(directories, policy=None):
    atlas = None
    sources = []
    for directory in directories:
        manifest, messages, spec = read_touch(directory, require_complete=True)
        if manifest.get("metadata", {}).get("role") != "enrollment":
            raise ValueError("use recordings labeled enrollment; development/holdout probes stay separate")
        source = sequence_source(directory)
        if any(source["frames_sha256"] == old["frames_sha256"] for old in sources):
            raise ValueError("duplicate enrollment recording")
        if atlas is None:
            atlas = FeatureAtlas(spec, policy)
        elif atlas.frame_spec != spec:
            raise ValueError("enrollment sequences have different frame geometry")
        tracker = TouchTracker(spec)
        for message, boundary in sequence_events(messages):
            if boundary:
                tracker.discontinuity()
            if message.status is FrameStatus.VALID:
                tracker.observe(message.pixels, message.sequence)
        atlas.add_touch(tracker.observations)
        sources.append(source)
    if atlas is None or not atlas.keyframes:
        raise ValueError("enrollment produced no usable keyframes")
    return atlas, sources


def evaluate_sequence(atlas, atlas_manifest, directory, expected):
    if expected not in {"genuine", "impostor"}:
        raise ValueError("expected must be genuine or impostor")
    manifest, messages, spec = read_touch(directory)
    if spec != atlas.frame_spec:
        raise ValueError("probe geometry differs from enrollment")
    source = sequence_source(directory)
    if any(source["frames_sha256"] == old["frames_sha256"]
           for old in atlas_manifest["sources"]):
        raise ValueError("enrollment recordings cannot serve as evaluation probes")
    matcher = StreamingAtlasMatcher(atlas)
    updates = []
    latencies = []
    first_evidence_ms = None
    first_evidence_processing_ms = None
    started = next(item.captured_started for item in messages if item.status is FrameStatus.VALID)
    for message, boundary in sequence_events(messages):
        if boundary:
            matcher.discontinuity()
        if message.status is not FrameStatus.VALID:
            continue
        begin = time.perf_counter()
        result = matcher.observe(message.pixels, message.sequence)
        elapsed_ms = (time.perf_counter() - begin) * 1000
        latencies.append(elapsed_ms)
        result["processing_ms"] = elapsed_ms
        result["capture_elapsed_ms"] = (message.captured_finished - started) * 1000
        updates.append(result)
        if result["evidence_sufficient"] and first_evidence_ms is None:
            first_evidence_ms = result["capture_elapsed_ms"]
            first_evidence_processing_ms = sum(latencies)
    candidate_match = first_evidence_ms is not None
    valid_trial = complete_touch(manifest, messages)
    return {
        "schema_version": 1,
        "matcher_version": SEQUENCE_MATCHER_VERSION,
        "experimental": True,
        "implementation": implementation_fingerprint(),
        "enrollment_implementation": atlas_manifest["implementation"],
        "promotable": False,
        "expected": expected,
        "candidate_match": candidate_match,
        "valid_trial": valid_trial,
        "candidate_correct": candidate_match == (expected == "genuine") if valid_trial else None,
        "time_to_evidence_capture_ms": first_evidence_ms,
        "time_to_evidence_processing_ms": first_evidence_processing_ms,
        "replay_mode": "all_recorded_frames",
        "processing_p95_ms": float(np.percentile(latencies, 95)),
        "source": source,
        "source_complete": bool(manifest.get("complete")),
        "source_role": manifest.get("metadata", {}).get("role"),
        "atlas_payload_sha256": atlas_manifest["payload_sha256"],
        "atlas_summary": atlas.summary(),
        "atlas_policy": asdict(atlas.policy),
        "updates": updates,
    }
