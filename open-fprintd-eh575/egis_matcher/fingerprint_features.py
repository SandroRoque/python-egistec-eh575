"""Fingerprint-specific ridge and minutiae features.

This module is deliberately independent of USB, persistence, and Linux.  It is
an experimental backend for the small EH575 frames; callers must treat a low
feature count as unscorable rather than as a match.
"""

from dataclasses import dataclass

import cv2
import numpy as np
from skimage.morphology import skeletonize

from egis_matcher.frame import FrameSpec


@dataclass(frozen=True)
class Minutia:
    x: float
    y: float
    angle: float
    kind: str
    quality: float
    descriptor: tuple[float, ...] = ()


@dataclass(frozen=True)
class FingerprintFeatures:
    image: np.ndarray
    mask: np.ndarray
    orientation: np.ndarray
    reliability: np.ndarray
    ridge_frequency: float | None
    enhanced: np.ndarray
    minutiae: tuple[Minutia, ...]
    quality: float
    minutiae_candidates: int = 0
    boundary_rejected: int = 0


class FingerprintFeatureExtractor:
    """Extract ridge-flow and minutia features from one raw sensor frame."""

    def __init__(self, frame_spec=None, block_size=8, edge_margin=5):
        self.frame_spec = frame_spec or FrameSpec(103, 52)
        self.block_size = int(block_size)
        self.edge_margin = int(edge_margin)
        if self.edge_margin < 1:
            raise ValueError("edge_margin must be positive")

    def raw_frame_to_image(self, raw):
        if len(raw) != self.frame_spec.byte_count:
            raise ValueError("invalid fingerprint frame size")
        return np.frombuffer(bytes(raw), dtype=np.uint8).reshape(
            self.frame_spec.height, self.frame_spec.width)

    def extract(self, raw_or_image):
        if isinstance(raw_or_image, (bytes, bytearray, memoryview)):
            image = self.raw_frame_to_image(raw_or_image)
        else:
            candidate = np.asarray(raw_or_image)
            image = (self.raw_frame_to_image(candidate.tobytes())
                     if candidate.ndim == 1 else
                     np.asarray(candidate, dtype=np.uint8))
            if image.shape != (self.frame_spec.height, self.frame_spec.width):
                raise ValueError("invalid fingerprint image shape")
        normalized = self._normalize(image)
        mask = self._foreground_mask(normalized)
        orientation, reliability = self._orientation(normalized)
        enhanced = self._enhance(normalized, orientation, reliability, mask)
        minutiae, minutiae_candidates, boundary_rejected = self._minutiae(
            enhanced, mask, orientation, reliability)
        ridge_frequency = self._ridge_frequency(normalized, mask)
        quality = self._quality(normalized, mask, reliability, minutiae)
        return FingerprintFeatures(
            normalized, mask, orientation, reliability, ridge_frequency,
            enhanced, tuple(minutiae), quality, minutiae_candidates,
            boundary_rejected)

    @staticmethod
    def _normalize(image):
        low, high = np.percentile(image, (2, 98))
        if high <= low:
            return np.zeros_like(image)
        return np.clip((image.astype(np.float32) - low) * 255.0 /
                       (high - low), 0, 255).astype(np.uint8)

    def _foreground_mask(self, image):
        image_f = image.astype(np.float32)
        mean = cv2.blur(image_f, (9, 9))
        mean_sq = cv2.blur(image_f * image_f, (9, 9))
        variance = np.maximum(0.0, mean_sq - mean * mean)
        mask = (variance > max(18.0, float(np.percentile(variance, 45)))).astype(np.uint8)
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        return mask

    def _orientation(self, image):
        image_f = cv2.GaussianBlur(image.astype(np.float32), (5, 5), 0)
        gx = cv2.Sobel(image_f, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(image_f, cv2.CV_32F, 0, 1, ksize=3)
        gxx = cv2.blur(gx * gx - gy * gy, (self.block_size, self.block_size))
        gxy = cv2.blur(2 * gx * gy, (self.block_size, self.block_size))
        orientation = 0.5 * np.arctan2(gxy, gxx)
        energy = cv2.blur(gx * gx + gy * gy, (self.block_size, self.block_size))
        reliability = np.sqrt(gxx * gxx + gxy * gxy) / (energy + 1e-6)
        return orientation.astype(np.float32), np.clip(reliability, 0, 1).astype(np.float32)

    def _enhance(self, image, orientation, reliability, mask):
        # A compact orientation-agnostic ridge enhancement.  Orientation is
        # retained for diagnostics and minutia validation; several directions
        # avoid inventing a preferred ridge direction at this stage.
        blurred = cv2.GaussianBlur(image, (3, 3), 0)
        detail = cv2.subtract(image, blurred)
        enhanced = cv2.normalize(detail, None, 0, 255, cv2.NORM_MINMAX)
        enhanced[mask == 0] = 0
        return enhanced.astype(np.uint8)

    def _ridge_frequency(self, image, mask):
        values = []
        # Frequency estimation is intentionally conservative for the small
        # sensor: return None until a row has enough alternating ridge energy.
        for y in range(image.shape[0]):
            signal = image[y][mask[y] > 0].astype(np.float32)
            if signal.size < 16:
                continue
            centered = signal - signal.mean()
            crossings = np.count_nonzero(np.diff(np.signbit(centered)))
            if crossings >= 4:
                values.append(float(crossings / max(1, signal.size)))
        return float(np.median(values)) if values else None

    def _minutiae(self, enhanced, mask, orientation, reliability):
        binary = cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 9, 2)
        skeleton = skeletonize((binary > 0) & (mask > 0))
        result = []
        candidates = 0
        boundary_rejected = 0
        height, width = skeleton.shape
        boundary_distance = cv2.distanceTransform(
            (mask > 0).astype(np.uint8), cv2.DIST_L2, 3)
        for y in range(1, height - 1):
            for x in range(1, width - 1):
                if not skeleton[y, x]:
                    continue
                neighbours = (
                    skeleton[y - 1, x],
                    skeleton[y - 1, x + 1],
                    skeleton[y, x + 1],
                    skeleton[y + 1, x + 1],
                    skeleton[y + 1, x],
                    skeleton[y + 1, x - 1],
                    skeleton[y, x - 1],
                    skeleton[y - 1, x - 1],
                )
                transitions = sum(
                    bool(neighbours[index]) != bool(neighbours[(index + 1) % 8])
                    for index in range(8)
                ) // 2
                if transitions not in (1, 3):
                    continue
                candidates += 1
                physical_distance = min(x, y, width - 1 - x, height - 1 - y)
                if (physical_distance <= self.edge_margin or
                        boundary_distance[y, x] <= self.edge_margin):
                    boundary_rejected += 1
                    continue
                kind = "ending" if transitions == 1 else "bifurcation"
                block_y = min(height - 1, y // self.block_size * self.block_size)
                block_x = min(width - 1, x // self.block_size * self.block_size)
                q = float(reliability[block_y, block_x])
                if q < 0.15:
                    continue
                result.append(Minutia(float(x), float(y),
                                      float(orientation[block_y, block_x]), kind,
                                      q, self._local_descriptor(enhanced, x, y)))
        return self._deduplicate_minutiae(result), candidates, boundary_rejected

    @staticmethod
    def _local_descriptor(image, x, y, radius=4):
        patch = image[y - radius:y + radius + 1,
                      x - radius:x + radius + 1].astype(np.float32)
        patch = cv2.resize(patch, (5, 5), interpolation=cv2.INTER_AREA)
        patch -= float(patch.mean())
        norm = float(np.linalg.norm(patch))
        if norm > 1e-6:
            patch /= norm
        return tuple(float(value) for value in patch.ravel())

    @staticmethod
    def _deduplicate_minutiae(items, radius=3.0):
        selected = []
        for item in sorted(items, key=lambda value: value.quality, reverse=True):
            if all((item.x - old.x) ** 2 + (item.y - old.y) ** 2 >= radius ** 2
                   for old in selected):
                selected.append(item)
        return selected

    @staticmethod
    def _quality(image, mask, reliability, minutiae):
        coverage = float(np.mean(mask > 0))
        contrast = min(1.0, float(np.std(image)) / 64.0)
        orientation = float(np.mean(reliability[mask > 0])) if np.any(mask) else 0.0
        minutia_support = min(1.0, len(minutiae) / 12.0)
        return float(0.25 * coverage + 0.25 * contrast +
                     0.30 * orientation + 0.20 * minutia_support)
