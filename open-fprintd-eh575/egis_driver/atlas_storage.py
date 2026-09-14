"""Private numeric atlas artifacts; never used by production template loading."""

import hashlib
import importlib
import json
import os
import zipfile
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

from egis_matcher.atlas import (
    ATLAS_SCHEMA_VERSION, SEQUENCE_MATCHER_VERSION,
    AtlasKeyframe, AtlasPolicy, FeatureAtlas, feature_cells,
)
from egis_matcher.frame import FrameSpec
from egis_matcher.sequence import Registration, TrackedObservation


MAX_ATLAS_BYTES = 64 * 1024 * 1024


def implementation_fingerprint():
    digest = hashlib.sha256()
    for name in ("egis_matcher.atlas", "egis_matcher.sequence",
                 "egis_matcher.image_features", "egis_matcher.frame"):
        module = importlib.import_module(name)
        digest.update(name.encode() + b"\0")
        digest.update(Path(module.__file__).read_bytes())
    return {"source_sha256": digest.hexdigest(),
            "opencv_version": cv2.__version__, "numpy_version": np.__version__}


def write_private_json(path, data):
    path = Path(path)
    # Output paths are new artifacts, not silent replacements of earlier runs.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(data, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def save_atlas(atlas, directory, sources, finger=None, *, experimental=True,
               include_raw=True):
    if not atlas.keyframes:
        raise ValueError("enrollment produced no usable keyframes")
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    arrays = {}
    frames = []
    for index, keyframe in enumerate(atlas.keyframes):
        item = keyframe.observation
        if include_raw:
            arrays[f"raw_{index}"] = np.frombuffer(item.raw_frame, dtype=np.uint8)
        arrays[f"image_{index}"] = item.image
        arrays[f"descriptors_{index}"] = item.descriptors
        arrays[f"points_{index}"] = np.float32([
            [*point.pt, point.size, point.angle, point.response, point.octave, point.class_id]
            for point in item.keypoints])
        arrays[f"transform_{index}"] = item.registration.transform
        frames.append({
            "touch": keyframe.touch,
            "sequence": item.sequence,
            "quality": item.quality,
            "component": item.registration.component,
            "reference": item.registration.reference,
        })
    temporary = directory / "atlas.npz.tmp"
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        np.savez_compressed(stream, **arrays)
    payload = directory / "atlas.npz"
    os.replace(temporary, payload)
    manifest = {
        "schema_version": ATLAS_SCHEMA_VERSION,
        "matcher_version": SEQUENCE_MATCHER_VERSION,
        "experimental": bool(experimental),
        "contains_raw_frames": bool(include_raw),
        "implementation": implementation_fingerprint(),
        "frame_spec": asdict(atlas.frame_spec),
        "policy": asdict(atlas.policy),
        "summary": atlas.summary(),
        "sources": sources,
        "finger": finger,
        "frames": frames,
        "payload_sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
    }
    write_private_json(directory / "manifest.json", manifest)
    return manifest


def load_atlas(directory, *, require_experimental=True):
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    payload = directory / "atlas.npz"
    if manifest_path.stat().st_size > 1024 * 1024 or payload.stat().st_size > MAX_ATLAS_BYTES:
        raise ValueError("atlas exceeds artifact size budget")
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("schema_version") != ATLAS_SCHEMA_VERSION or
            manifest.get("matcher_version") != SEQUENCE_MATCHER_VERSION or
            (require_experimental is not None and
             manifest.get("experimental") is not require_experimental)):
        raise ValueError("unsupported atlas schema or matcher version; re-enroll")
    if hashlib.sha256(payload.read_bytes()).hexdigest() != manifest["payload_sha256"]:
        raise ValueError("atlas checksum mismatch")
    spec = FrameSpec(**manifest["frame_spec"])
    if spec.dtype != "uint8" or not (0 < spec.width <= 1024 and 0 < spec.height <= 1024):
        raise ValueError("invalid atlas frame geometry")
    policy = AtlasPolicy(**manifest["policy"])
    frames = manifest["frames"]
    if not 0 < len(frames) <= min(policy.max_keyframes, 512):
        raise ValueError("invalid atlas keyframe count")
    with zipfile.ZipFile(payload) as archive:
        if sum(member.file_size for member in archive.infolist()) > MAX_ATLAS_BYTES:
            raise ValueError("expanded atlas exceeds size budget")
    atlas = FeatureAtlas(spec, policy)
    with np.load(payload, allow_pickle=False) as arrays:
        for index, frame in enumerate(frames):
            raw = arrays[f"raw_{index}"] if manifest.get(
                "contains_raw_frames", True) else None
            image = arrays[f"image_{index}"]
            descriptors = arrays[f"descriptors_{index}"]
            points = arrays[f"points_{index}"]
            transform = arrays[f"transform_{index}"]
            if ((raw is not None and
                 (raw.dtype != np.uint8 or raw.shape != (spec.byte_count,))) or
                    image.dtype != np.uint8 or image.shape != (spec.height, spec.width) or
                    points.ndim != 2 or points.shape[1] != 7 or
                    not policy.min_features <= len(points) <= 10000 or
                    descriptors.dtype != np.float32 or descriptors.shape != (len(points), 128) or
                    transform.shape != (3, 3) or
                    not all(np.isfinite(array).all() for array in (points, descriptors, transform)) or
                    not np.allclose(transform[2], [0, 0, 1]) or
                    abs(np.linalg.det(transform)) < 1e-6):
                raise ValueError("invalid atlas feature arrays")
            quality = float(frame["quality"])
            if not np.isfinite(quality) or not 0 <= quality <= 1:
                raise ValueError("invalid atlas frame quality")
            keypoints = tuple(cv2.KeyPoint(
                float(p[0]), float(p[1]), float(p[2]), float(p[3]),
                float(p[4]), int(p[5]), int(p[6])) for p in points)
            registration = Registration(
                True, int(frame["component"]), frame["reference"],
                transform.astype(np.float32))
            observation = TrackedObservation(
                int(frame["sequence"]), image, keypoints, descriptors,
                quality, registration, raw.tobytes() if raw is not None else None)
            keyframe = AtlasKeyframe(int(frame["touch"]), observation)
            atlas.keyframes.append(keyframe)
            atlas._coverage.setdefault(keyframe.component, set()).update(
                feature_cells(observation, policy.cell_size))
    summary = manifest["summary"]
    for name in ("touches", "frames_seen", "weak_frames", "redundant_frames"):
        setattr(atlas, name, int(summary[name]))
    return atlas, manifest
