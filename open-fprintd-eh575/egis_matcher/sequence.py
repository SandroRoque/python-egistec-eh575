import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from egis_matcher.image_features import ImageFeatureExtractor


@dataclass(frozen=True)
class Registration:
    accepted: bool
    component: int
    reference: int | None
    transform: np.ndarray
    matches: int = 0
    inliers: int = 0
    inlier_ratio: float = 0.0
    ridge_score: float = 0.0
    spatial_cells: int = 0
    confidence: float = 0.0
    reason: str | None = None


@dataclass
class TrackedObservation:
    sequence: int
    image: np.ndarray
    keypoints: tuple
    descriptors: np.ndarray | None
    quality: float
    registration: Registration


class TouchTracker:
    """Registers ordered small-sensor frames while retaining source evidence."""

    def __init__(self, frame_spec, features=None, max_references=3,
                 ratio=0.75, min_inliers=6, min_inlier_ratio=0.35,
                 min_spatial_cells=3, min_ridge_score=0.25):
        self.features = features or ImageFeatureExtractor(frame_spec=frame_spec)
        self.frame_spec = frame_spec
        self.max_references = max_references
        self.ratio = ratio
        self.min_inliers = min_inliers
        self.min_inlier_ratio = min_inlier_ratio
        self.min_spatial_cells = min_spatial_cells
        self.min_ridge_score = min_ridge_score
        self.observations = []
        self.discontinuities = 0
        self._next_component = 0
        self._force_component = False

    def begin_touch(self):
        self.observations = []
        self.discontinuities = 0
        self._next_component = 0
        self._force_component = False

    def discontinuity(self):
        self.discontinuities += 1
        self._force_component = True

    def end_touch(self):
        return self.summary()

    def observe(self, raw_frame, sequence=None):
        image_raw = self.features.raw_frame_to_image(raw_frame)
        image = self.features.preprocess(image_raw)
        keypoints, descriptors = self.features.detect_features(image)
        keypoints = tuple(keypoints or ())
        quality = float(self.features.frame_quality(image_raw))
        sequence = len(self.observations) + 1 if sequence is None else sequence

        reference_start = max(0, len(self.observations) - self.max_references)
        registration = None
        if (not self._force_component and descriptors is not None and
                len(keypoints) >= 4):
            candidates = [
                self._register(image, keypoints, descriptors, reference, index)
                for index in range(len(self.observations) - 1,
                                   reference_start - 1, -1)
                for reference in (self.observations[index],)
                if reference.descriptors is not None
            ]
            accepted = [candidate for candidate in candidates if candidate.accepted]
            if accepted:
                registration = max(accepted, key=lambda item: item.confidence)

        if registration is None:
            if self.observations:
                self._next_component += 1
            registration = Registration(
                accepted=True,
                component=self._next_component,
                reference=None,
                transform=np.eye(3, dtype=np.float32),
                reason="component_root" if self.observations else "touch_root",
            )
        self._force_component = False

        observation = TrackedObservation(
            sequence,
            image,
            keypoints,
            descriptors,
            quality,
            registration,
        )
        self.observations.append(observation)
        return observation

    def _register(self, image, keypoints, descriptors, reference, reference_index):
        matcher = cv2.BFMatcher(self.features.descriptor_norm, crossCheck=False)
        pairs = matcher.knnMatch(descriptors, reference.descriptors, k=2)
        good = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < self.ratio * pair[1].distance]
        identity = np.eye(3, dtype=np.float32)
        if len(good) < 4:
            return Registration(False, reference.registration.component,
                                reference_index, identity, matches=len(good),
                                reason="insufficient_matches")
        source = np.float32([keypoints[item.queryIdx].pt for item in good])
        target = np.float32([reference.keypoints[item.trainIdx].pt for item in good])
        affine, mask = cv2.estimateAffinePartial2D(
            source, target, method=cv2.RANSAC, ransacReprojThreshold=4.0)
        if affine is None or mask is None:
            return Registration(False, reference.registration.component,
                                reference_index, identity, matches=len(good),
                                reason="no_transform")
        inliers = int(mask.sum())
        ratio = inliers / len(good)
        inlier_source = source[mask.ravel().astype(bool)]
        cells = self._spatial_cells(inlier_source)
        scale = math.sqrt(float(affine[0, 0] ** 2 + affine[1, 0] ** 2))
        angle = abs(math.degrees(math.atan2(float(affine[1, 0]), float(affine[0, 0]))))
        local = np.vstack([affine, [0.0, 0.0, 1.0]]).astype(np.float32)
        transform = reference.registration.transform @ local
        ridge = self.features.ridge_consistency(
            image,
            {
                "image": reference.image,
                "ridge": self.features.template_descriptor(reference.image),
            },
            local,
        )
        valid_geometry = 0.70 <= scale <= 1.30 and angle <= 30.0
        accepted = all((
            inliers >= self.min_inliers,
            ratio >= self.min_inlier_ratio,
            cells >= self.min_spatial_cells,
            ridge["ridge_score"] >= self.min_ridge_score,
            valid_geometry,
        ))
        confidence = (
            min(1.0, inliers / 20.0) * 0.35 +
            min(1.0, ratio) * 0.25 +
            max(0.0, ridge["ridge_score"]) * 0.25 +
            min(1.0, cells / 6.0) * 0.15
        )
        return Registration(
            accepted,
            reference.registration.component,
            reference_index,
            transform,
            len(good),
            inliers,
            ratio,
            float(ridge["ridge_score"]),
            cells,
            confidence,
            None if accepted else "validation_failed",
        )

    def _spatial_cells(self, points):
        if len(points) == 0:
            return 0
        width = max(1, self.frame_spec.width)
        height = max(1, self.frame_spec.height)
        return len({
            (
                min(2, int(point[0] * 3 / width)),
                min(1, int(point[1] * 2 / height)),
            )
            for point in points
        })

    def summary(self):
        components = sorted({
            observation.registration.component
            for observation in self.observations
        })
        registered = sum(
            observation.registration.reference is not None
            for observation in self.observations
        )
        return {
            "frames": len(self.observations),
            "registered_frames": registered,
            "components": len(components),
            "discontinuities": self.discontinuities,
            "mean_confidence": float(np.mean([
                observation.registration.confidence
                for observation in self.observations
                if observation.registration.reference is not None
            ])) if registered else 0.0,
        }

    def render_components(self):
        return {
            component: self._render_component(component)
            for component in sorted({
                observation.registration.component
                for observation in self.observations
            })
        }

    def _render_component(self, component):
        observations = [
            item for item in self.observations
            if item.registration.component == component
        ]
        if not observations:
            return np.zeros((0, 0), dtype=np.uint8)
        corners = np.float32([
            [0, 0], [self.frame_spec.width, 0],
            [self.frame_spec.width, self.frame_spec.height],
            [0, self.frame_spec.height],
        ]).reshape(-1, 1, 2)
        transformed = [
            cv2.perspectiveTransform(corners, item.registration.transform)
            for item in observations
        ]
        points = np.concatenate(transformed).reshape(-1, 2)
        minimum = np.floor(points.min(axis=0)).astype(int)
        maximum = np.ceil(points.max(axis=0)).astype(int)
        size = maximum - minimum
        max_size = np.array([
            self.frame_spec.width * 4,
            self.frame_spec.height * 4,
        ])
        if np.any(size <= 0) or np.any(size > max_size):
            raise ValueError("mosaic bounds are invalid")
        shift = np.array([
            [1.0, 0.0, -minimum[0]],
            [0.0, 1.0, -minimum[1]],
            [0.0, 0.0, 1.0],
        ], dtype=np.float32)
        mosaic = np.zeros((size[1], size[0]), dtype=np.uint8)
        weights = np.full(mosaic.shape, -1.0, dtype=np.float32)
        source_mask = np.ones(
            (self.frame_spec.height, self.frame_spec.width), dtype=np.uint8)
        for item in observations:
            transform = shift @ item.registration.transform
            output_size = (int(size[0]), int(size[1]))
            warped = cv2.warpPerspective(item.image, transform, output_size)
            mask = cv2.warpPerspective(
                source_mask, transform, output_size, flags=cv2.INTER_NEAREST)
            replace = (mask > 0) & (item.quality > weights)
            mosaic[replace] = warped[replace]
            weights[replace] = item.quality
        return mosaic
