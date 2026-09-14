"""Experimental ridge/minutiae matcher independent of the sensor service."""

from dataclasses import dataclass
import math

import cv2
import numpy as np

from egis_matcher.fingerprint_features import (
    FingerprintFeatureExtractor,
    FingerprintFeatures,
    Minutia,
)


FINGERPRINT_MATCHER_VERSION = "ridge-minutiae-v1"


@dataclass(frozen=True)
class FingerprintTemplate:
    features: tuple[FingerprintFeatures, ...]
    version: str = FINGERPRINT_MATCHER_VERSION


@dataclass(frozen=True)
class FingerprintMatch:
    matched: bool
    score: float
    reason: str
    metrics: dict


class FingerprintMatcher:
    """Compare a probe sequence against a multi-frame derived template.

    This is intentionally replay-only.  It never opens a device, reads a
    service database, or decides an OS authentication result.
    """

    def __init__(self, extractor=None, min_inliers=6, min_score=0.34):
        self.extractor = extractor or FingerprintFeatureExtractor()
        self.min_inliers = int(min_inliers)
        self.min_score = float(min_score)

    def build_template(self, frames):
        features = tuple(
            feature for feature in (self.extractor.extract(frame) for frame in frames)
            if feature.quality >= 0.18 and len(feature.minutiae) >= 3
        )
        return FingerprintTemplate(features)

    @staticmethod
    def template_payload(template):
        payload = {"rm_num_templates": np.array(len(template.features), dtype=np.int32)}
        for index, feature in enumerate(template.features):
            rows = []
            for minutia in feature.minutiae:
                descriptor = tuple(minutia.descriptor)
                if len(descriptor) != 25:
                    descriptor = (0.0,) * 25
                rows.append((minutia.x, minutia.y, minutia.angle,
                             0.0 if minutia.kind == "ending" else 1.0,
                             minutia.quality, *descriptor))
            payload[f"rm_minutiae_{index}"] = np.asarray(rows, dtype=np.float32).reshape(-1, 30)
            payload[f"rm_quality_{index}"] = np.float32(feature.quality)
            payload[f"rm_frequency_{index}"] = np.float32(
                feature.ridge_frequency if feature.ridge_frequency is not None else np.nan)
        return payload

    def template_from_payload(self, payload):
        count = int(payload["rm_num_templates"])
        shape = (self.extractor.frame_spec.height, self.extractor.frame_spec.width)
        empty_u8 = np.zeros(shape, dtype=np.uint8)
        empty_f32 = np.zeros(shape, dtype=np.float32)
        features = []
        for index in range(count):
            rows = np.asarray(payload[f"rm_minutiae_{index}"], dtype=np.float32)
            minutiae = tuple(Minutia(
                float(row[0]), float(row[1]), float(row[2]),
                "ending" if row[3] < 0.5 else "bifurcation",
                float(row[4]), tuple(float(value) for value in row[5:30]))
                for row in rows)
            frequency = float(payload[f"rm_frequency_{index}"])
            features.append(FingerprintFeatures(
                empty_u8, empty_u8, empty_f32, empty_f32,
                None if np.isnan(frequency) else frequency,
                empty_u8, minutiae, float(payload[f"rm_quality_{index}"])))
        return FingerprintTemplate(tuple(features))

    def match(self, frames, template):
        probes = self.extract_probe(frames)
        return self.match_features(probes, template)

    def extract_probe(self, frames):
        return tuple(
            feature for feature in (self.extractor.extract(frame) for frame in frames)
            if feature.quality >= 0.12 and len(feature.minutiae) >= 3
        )

    def match_features(self, probes, template):
        if not probes or not template.features:
            return FingerprintMatch(False, 0.0, "insufficient_features", {
                "probe_frames": len(probes), "template_frames": len(template.features),
            })
        best = max((self._compare(probe, reference)
                    for probe in probes for reference in template.features),
                   key=lambda result: result.score)
        return best

    def _compare(self, probe, reference):
        pairs = self._candidate_pairs(probe, reference)
        if len(pairs) < 3:
            return FingerprintMatch(False, 0.0, "no_valid_alignment", {
                "candidate_pairs": len(pairs), "probe_minutiae": len(probe.minutiae),
                "template_minutiae": len(reference.minutiae),
            })
        source = np.float32([[a.x, a.y] for a, _ in pairs])
        target = np.float32([[b.x, b.y] for _, b in pairs])
        matrix, inlier_mask = cv2.estimateAffinePartial2D(
            source, target, method=cv2.RANSAC, ransacReprojThreshold=4.0,
            maxIters=200, confidence=0.98)
        if matrix is None or inlier_mask is None:
            return FingerprintMatch(False, 0.0, "no_valid_alignment", {
                "candidate_pairs": len(pairs), "inliers": 0,
            })
        inliers = int(np.count_nonzero(inlier_mask))
        inlier_pairs = [pair for pair, flag in zip(pairs, inlier_mask.ravel()) if flag]
        rotation = math.atan2(float(matrix[1, 0]), float(matrix[0, 0]))
        scale = math.hypot(float(matrix[0, 0]), float(matrix[1, 0]))
        orientation = self._orientation_agreement(inlier_pairs, rotation)
        spatial_coverage = self._spatial_coverage(inlier_pairs, probe.image.shape)
        descriptor_agreement = float(np.mean([
            self._descriptor_similarity(a, b) for a, b in inlier_pairs
        ])) if inlier_pairs else 0.0
        coverage = min(1.0, inliers / max(8.0, min(len(probe.minutiae), len(reference.minutiae)) * 0.55))
        score = float(0.35 * coverage + 0.20 * orientation +
                      0.25 * descriptor_agreement + 0.20 * spatial_coverage)
        geometry_valid = 0.82 <= scale <= 1.18 and spatial_coverage >= 0.08
        matched = (inliers >= self.min_inliers and geometry_valid and
                   descriptor_agreement >= 0.58 and score >= self.min_score)
        return FingerprintMatch(matched, score, "none" if matched else "weak_alignment", {
            "candidate_pairs": len(pairs), "inliers": inliers,
            "orientation_agreement": orientation, "probe_quality": probe.quality,
            "template_quality": reference.quality, "minutiae_score": score,
            "descriptor_agreement": descriptor_agreement,
            "spatial_coverage": spatial_coverage,
            "transform_rotation": rotation,
            "transform_scale": scale,
            "probe_minutiae_candidates": probe.minutiae_candidates,
            "probe_boundary_rejected": probe.boundary_rejected,
            "probe_minutiae_retained": len(probe.minutiae),
            "template_minutiae_candidates": reference.minutiae_candidates,
            "template_boundary_rejected": reference.boundary_rejected,
            "template_minutiae_retained": len(reference.minutiae),
            "ridge_frequency_probe": probe.ridge_frequency,
            "ridge_frequency_template": reference.ridge_frequency,
        })

    @staticmethod
    def _candidate_pairs(probe, reference):
        pairs = []
        used = set()
        for left in probe.minutiae:
            candidates = [(index, right) for index, right in enumerate(reference.minutiae)
                          if (index not in used and right.kind == left.kind and
                              FingerprintMatcher._angle_distance(
                                  left.angle, right.angle, ridge=True) <= math.pi / 3)]
            if not candidates:
                continue
            index, right = min(candidates,
                               key=lambda item: -FingerprintMatcher._descriptor_similarity(
                                   left, item[1]))
            if FingerprintMatcher._descriptor_similarity(left, right) >= 0.45:
                pairs.append((left, right))
                used.add(index)
        return pairs

    @staticmethod
    def _angle_distance(left, right, ridge=False):
        period = math.pi if ridge else 2 * math.pi
        return abs((left - right + period / 2) % period - period / 2)

    @staticmethod
    def _orientation_agreement(pairs, rotation=0.0):
        if not pairs:
            return 0.0
        return float(np.mean([
            math.cos(FingerprintMatcher._angle_distance(
                a.angle + rotation, b.angle, ridge=True))
            for a, b in pairs
        ]))

    @staticmethod
    def _descriptor_similarity(left, right):
        if not left.descriptor or not right.descriptor:
            return 1.0
        return float(np.clip(np.dot(left.descriptor, right.descriptor), -1.0, 1.0))

    @staticmethod
    def _spatial_coverage(pairs, image_shape):
        if len(pairs) < 3:
            return 0.0
        points = np.float32([[left.x, left.y] for left, _ in pairs])
        hull = cv2.convexHull(points)
        image_area = float(image_shape[0] * image_shape[1])
        return min(1.0, float(cv2.contourArea(hull)) / max(1.0, image_area * 0.35))
