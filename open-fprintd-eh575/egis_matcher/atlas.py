"""Experimental sequence enrollment and streaming spatial evidence.

This module has no persistence or sensor dependencies. Evidence sufficiency is a
research measurement, not a calibrated authentication decision.
"""

import math
from dataclasses import dataclass

import cv2
import numpy as np

from egis_matcher.sequence import TouchTracker, TrackedObservation


ATLAS_SCHEMA_VERSION = 1
SEQUENCE_MATCHER_VERSION = "sequence-atlas-experimental-v1"


@dataclass(frozen=True)
class AtlasPolicy:
    cell_size: int = 8
    min_quality: float = 0.2
    min_features: int = 6
    min_new_cells: int = 3
    min_evidence_cells: int = 12
    min_evidence_frames: int = 2
    min_motion_pixels: float = 8.0
    max_pose_error_pixels: float = 6.0
    max_keyframes: int = 256
    max_keyframes_per_component: int = 2

    def __post_init__(self):
        for name in ("cell_size", "min_features", "min_new_cells",
                     "min_evidence_cells", "min_evidence_frames", "max_keyframes",
                     "max_keyframes_per_component"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not math.isfinite(self.min_quality) or not 0 <= self.min_quality <= 1:
            raise ValueError("min_quality must be between zero and one")
        for value in (self.min_motion_pixels, self.max_pose_error_pixels):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("motion and pose limits must be positive")


@dataclass
class AtlasKeyframe:
    touch: int
    observation: TrackedObservation

    @property
    def component(self):
        return self.touch, self.observation.registration.component


def feature_cells(observation, cell_size, indices=None):
    points = [point.pt for point in observation.keypoints]
    if indices is not None:
        points = [points[index] for index in indices]
    if not points:
        return set()
    transformed = cv2.perspectiveTransform(
        np.float32(points).reshape(-1, 1, 2),
        observation.registration.transform).reshape(-1, 2)
    if not np.isfinite(transformed).all():
        raise ValueError("nonfinite atlas coordinates")
    return {tuple(map(int, point)) for point in np.floor(transformed / cell_size)}


class FeatureAtlas:
    """Source keyframes grouped by touch/component with spatial feature coverage."""

    def __init__(self, frame_spec, policy=None):
        self.frame_spec = frame_spec
        self.policy = policy or AtlasPolicy()
        self.keyframes = []
        self.touches = 0
        self.frames_seen = 0
        self.redundant_frames = 0
        self.weak_frames = 0
        self._coverage = {}

    def add_touch(self, observations):
        touch = self.touches
        # Build locally so budget errors cannot leave half an enrollment touch.
        selected = []
        coverage = {}
        seen = weak = redundant = 0
        for observation in observations:
            seen += 1
            if (observation.image.shape != (self.frame_spec.height, self.frame_spec.width)
                    or observation.raw_frame is None
                    or len(observation.raw_frame) != self.frame_spec.byte_count):
                raise ValueError("atlas observation has incompatible geometry or missing source")
            if (observation.quality < self.policy.min_quality or
                    observation.descriptors is None or
                    len(observation.keypoints) < self.policy.min_features):
                weak += 1
                continue
            keyframe = AtlasKeyframe(touch, observation)
            cells = feature_cells(observation, self.policy.cell_size)
            previous = coverage.setdefault(keyframe.component, set())
            selected_component_count = sum(
                item.component == keyframe.component for item in selected)
            if selected_component_count >= self.policy.max_keyframes_per_component:
                redundant += 1
                continue
            if previous and len(cells - previous) < self.policy.min_new_cells:
                redundant += 1
                continue
            selected.append(keyframe)
            previous.update(cells)
        if len(self.keyframes) + len(selected) > self.policy.max_keyframes:
            raise ValueError("enrollment exceeds atlas keyframe budget")
        self.keyframes.extend(selected)
        self._coverage.update(coverage)
        self.touches += 1
        self.frames_seen += seen
        self.weak_frames += weak
        self.redundant_frames += redundant

    def summary(self):
        return {
            "touches": self.touches,
            "frames_seen": self.frames_seen,
            "keyframes": len(self.keyframes),
            "weak_frames": self.weak_frames,
            "redundant_frames": self.redundant_frames,
            "components": len(self._coverage),
            # Components have unrelated origins. This is a per-component total,
            # never a claim of unique fingerprint area across separate touches.
            "feature_cells_by_component": {
                f"{touch}:{component}": len(cells)
                for (touch, component), cells in self._coverage.items()
            },
        }


class StreamingAtlasMatcher:
    """Reuse frame features and admit evidence only from new supported regions."""

    def __init__(self, atlas, tracker=None):
        if not atlas.keyframes:
            raise ValueError("cannot match an empty atlas")
        self.atlas = atlas
        self.policy = atlas.policy
        self.tracker = tracker or TouchTracker(atlas.frame_spec)
        self._corners = np.float32([
            [0, 0], [atlas.frame_spec.width, 0],
            [atlas.frame_spec.width, atlas.frame_spec.height],
            [0, atlas.frame_spec.height],
        ]).reshape(-1, 1, 2)
        self.frames_seen = 0
        self._reset_evidence()

    def _reset_evidence(self):
        self._component = None
        self._live_component = None
        self._anchor = None
        self._last_pose = None
        self._cells = set()
        self._admitted = 0

    def discontinuity(self):
        self.tracker.discontinuity()
        self._reset_evidence()

    def begin_touch(self):
        self.tracker.begin_touch()
        self.frames_seen = 0
        self._reset_evidence()

    def _distance(self, first, second):
        a = cv2.perspectiveTransform(self._corners, first)
        b = cv2.perspectiveTransform(self._corners, second)
        return float(np.sqrt(np.mean(np.sum((a - b) ** 2, axis=2))))

    def observe(self, raw_frame, sequence=None):
        observation = self.tracker.observe(raw_frame, sequence)
        self.frames_seen += 1
        if observation.registration.component != self._live_component:
            self._reset_evidence()
            self._live_component = observation.registration.component
        if observation.quality < self.policy.min_quality:
            self._reset_evidence()
            return self._result(observation, "weak_frame")
        candidates = []
        for index, keyframe in enumerate(self.atlas.keyframes):
            registration = self.tracker.register(
                observation, keyframe.observation, index)
            if registration.accepted:
                candidates.append((registration, keyframe))
        if not candidates:
            self._reset_evidence()
            return self._result(observation, "no_alignment")
        registration, keyframe = max(candidates, key=lambda pair: pair[0].confidence)
        root_to_atlas = registration.transform @ np.linalg.inv(
            observation.registration.transform)
        if keyframe.component != self._component:
            self._reset_evidence()
            self._live_component = observation.registration.component
            self._component = keyframe.component
            self._anchor = root_to_atlas
        elif self._distance(root_to_atlas, self._anchor) > self.policy.max_pose_error_pixels:
            self._reset_evidence()
            return self._result(observation, "inconsistent_pose", registration)
        cells = feature_cells(keyframe.observation, self.policy.cell_size,
                              registration.target_indices)
        new_cells = cells - self._cells
        motion = (self._distance(registration.transform, self._last_pose)
                  if self._last_pose is not None else 0.0)
        novel = (len(new_cells) >= self.policy.min_new_cells and
                 (self._last_pose is None or motion >= self.policy.min_motion_pixels))
        if novel:
            self._cells.update(cells)
            self._admitted += 1
            self._last_pose = registration.transform
        return self._result(
            observation, "new_region" if novel else "redundant_region",
            registration, len(new_cells), motion)

    def _result(self, observation, reason, registration=None, new_cells=0, motion=0.0):
        return {
            "sequence": observation.sequence,
            "reason": reason,
            "frames_seen": self.frames_seen,
            "admitted_frames": self._admitted,
            "supported_cells": len(self._cells),
            "new_cells": new_cells,
            "motion_pixels": motion,
            "component": list(self._component) if self._component is not None else None,
            "inliers": registration.inliers if registration is not None else 0,
            "ridge_score": registration.ridge_score if registration is not None else 0.0,
            "evidence_sufficient": (
                self._admitted >= self.policy.min_evidence_frames and
                len(self._cells) >= self.policy.min_evidence_cells),
            "calibrated": False,
        }
