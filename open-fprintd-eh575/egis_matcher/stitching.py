"""Continuous fingerprint compositing; identity evidence is deliberately absent."""

from dataclasses import dataclass

import cv2
import numpy as np

from egis_matcher.sequence import TouchTracker


@dataclass(frozen=True)
class StitchedPrint:
    image: np.ndarray
    mask: np.ndarray
    coverage_pixels: int
    admitted_frames: int
    component: int
    registration_confidence: float

    def __post_init__(self):
        image = np.array(self.image, dtype=np.uint8, copy=True)
        mask = np.array(self.mask, dtype=np.uint8, copy=True)
        image.setflags(write=False)
        mask.setflags(write=False)
        object.__setattr__(self, "image", image)
        object.__setattr__(self, "mask", mask)


class TouchStitcher:
    """Register swipe frames with SIFT and render one coherent fingerprint."""

    def __init__(self, frame_spec, tracker=None, border=4,
                 min_coverage_growth=0.10, max_canvas_factor=6,
                 blend="weighted"):
        self.frame_spec = frame_spec
        self.tracker = tracker or TouchTracker(frame_spec)
        self.border = int(border)
        self.min_coverage_growth = float(min_coverage_growth)
        self.max_canvas_factor = int(max_canvas_factor)
        self.blend = str(blend)
        if self.border < 0 or self.border * 2 >= min(frame_spec.width, frame_spec.height):
            raise ValueError("invalid stitcher border")
        if not 0 < self.min_coverage_growth <= 1:
            raise ValueError("coverage growth must be in (0, 1]")
        if self.blend not in {"weighted", "winner", "median"}:
            raise ValueError("blend must be weighted, winner, or median")
        self._last_snapshot_coverage = 0

    def begin_touch(self):
        self.tracker.begin_touch()
        self._last_snapshot_coverage = 0

    def discontinuity(self):
        self.tracker.discontinuity()
        self._last_snapshot_coverage = 0

    def observe(self, raw_frame, sequence=None):
        return self.tracker.observe(raw_frame, sequence)

    def snapshot(self, force=False):
        components = {}
        for item in self.tracker.observations:
            components.setdefault(item.registration.component, []).append(item)
        if not components:
            return None
        # Never join coordinate systems created by failed registration.
        component, observations = max(
            components.items(), key=lambda pair: (len(pair[1]), pair[0]))
        result = self._render(component, observations)
        if result is None:
            return None
        growth = result.coverage_pixels - self._last_snapshot_coverage
        threshold = max(1, int(max(
            self._last_snapshot_coverage,
            self.frame_spec.byte_count,
        ) * self.min_coverage_growth))
        if not force and self._last_snapshot_coverage and growth < threshold:
            return None
        self._last_snapshot_coverage = result.coverage_pixels
        return result

    def _render(self, component, observations):
        corners = np.float32([
            [self.border, self.border],
            [self.frame_spec.width - self.border, self.border],
            [self.frame_spec.width - self.border,
             self.frame_spec.height - self.border],
            [self.border, self.frame_spec.height - self.border],
        ]).reshape(-1, 1, 2)
        transformed = [
            cv2.perspectiveTransform(corners, item.registration.transform)
            for item in observations
        ]
        points = np.concatenate(transformed).reshape(-1, 2)
        minimum = np.floor(points.min(axis=0)).astype(int)
        maximum = np.ceil(points.max(axis=0)).astype(int)
        size = maximum - minimum
        limit = np.array([
            self.frame_spec.width * self.max_canvas_factor,
            self.frame_spec.height * self.max_canvas_factor,
        ])
        if np.any(size <= 0) or np.any(size > limit):
            return None
        shift = np.array([
            [1.0, 0.0, -minimum[0]],
            [0.0, 1.0, -minimum[1]],
            [0.0, 0.0, 1.0],
        ], dtype=np.float32)
        output_size = tuple(map(int, size))
        total = np.zeros((size[1], size[0]), dtype=np.float32)
        weights = np.zeros_like(total)
        selected = np.full(total.shape, 255, dtype=np.uint8)
        median_sources = []
        source_mask = np.zeros(
            (self.frame_spec.height, self.frame_spec.width), dtype=np.uint8)
        source_mask[
            self.border:self.frame_spec.height - self.border,
            self.border:self.frame_spec.width - self.border,
        ] = 1
        distance = cv2.distanceTransform(source_mask, cv2.DIST_L2, 3)
        distance /= max(float(distance.max()), 1.0)
        confidences = []
        for item in observations:
            transform = shift @ item.registration.transform
            image = item.image.astype(np.float32)
            valid = source_mask.astype(bool)
            if valid.any():
                mean, std = float(image[valid].mean()), float(image[valid].std())
                if std > 1.0:
                    image = np.clip((image - mean) * (40.0 / std) + 127.5, 0, 255)
            confidence = max(0.1, float(item.registration.confidence))
            warped = cv2.warpPerspective(image, transform, output_size)
            weight = cv2.warpPerspective(
                distance * confidence, transform, output_size)
            if self.blend == "weighted":
                total += warped * weight
                weights += weight
            elif self.blend == "winner":
                replace = weight > weights
                selected[replace] = np.clip(warped[replace], 0, 255).astype(np.uint8)
                weights[replace] = weight[replace]
            else:
                valid_warp = weight > 0.05
                median_sources.append(np.where(valid_warp, warped, np.nan))
                weights[valid_warp] = 1.0
            if item.registration.reference is not None:
                confidences.append(confidence)
        mask = weights > 0.05
        if not mask.any():
            return None
        composite = np.full(weights.shape, 255, dtype=np.uint8)
        if self.blend == "weighted":
            composite[mask] = np.clip(
                total[mask] / weights[mask], 0, 255).astype(np.uint8)
        elif self.blend == "winner":
            composite[mask] = selected[mask]
        else:
            samples = np.stack(median_sources)
            composite[mask] = np.clip(
                np.nanmedian(samples[:, mask], axis=0), 0, 255).astype(np.uint8)
        return StitchedPrint(
            composite,
            (mask.astype(np.uint8) * 255),
            int(mask.sum()),
            len(observations),
            component,
            float(np.mean(confidences)) if confidences else 0.0,
        )
