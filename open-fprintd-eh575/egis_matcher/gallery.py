"""Presentation-preserving opaque feature galleries and stream diagnostics."""

from dataclasses import dataclass, field
import time

import numpy as np
from skimage.metrics import structural_similarity as ssim

from egis_matcher.feature_engine import FeatureRecord, FingerprintImage
from egis_matcher.image_features import ImageFeatureExtractor
from egis_matcher.sequence import TouchTracker
from egis_matcher.stitching import TouchStitcher


GALLERY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GalleryPolicy:
    max_records_per_presentation: int = 24
    max_records_per_identity: int = 96
    min_quality: float = 0.12
    duplicate_ssim: float = 0.95
    frame_border: int = 4
    include_component_images: bool = True

    def __post_init__(self):
        if (self.max_records_per_presentation < 1 or
                self.max_records_per_identity < self.max_records_per_presentation):
            raise ValueError("invalid gallery record limits")
        if not 0 <= self.min_quality <= 1 or not 0 < self.duplicate_ssim <= 1:
            raise ValueError("invalid gallery quality policy")


@dataclass(frozen=True)
class GalleryEntry:
    presentation: int
    component: int
    sequence: int
    representation: str
    quality: float
    record: FeatureRecord


@dataclass
class PresentationGallery:
    identity: str
    engine: str
    engine_version: str
    engine_config: dict
    policy: GalleryPolicy = field(default_factory=GalleryPolicy)
    entries: list = field(default_factory=list)
    presentations: int = 0
    extraction_failures: int = 0

    def add(self, entry):
        if not isinstance(entry, GalleryEntry):
            raise TypeError("gallery accepts GalleryEntry values")
        if (entry.record.engine != self.engine or
                entry.record.version != self.engine_version):
            raise ValueError("gallery feature engine mismatch")
        if len(self.entries) >= self.policy.max_records_per_identity:
            return False
        self.entries.append(entry)
        return True


class GalleryBuilder:
    """Select complementary presentation evidence, then invoke the engine."""

    def __init__(self, engine, frame_spec, policy=None, features=None):
        self.engine = engine
        self.frame_spec = frame_spec
        self.policy = policy or GalleryPolicy()
        self.features = features or ImageFeatureExtractor(frame_spec=frame_spec)

    def build(self, identity, touch_groups):
        config = self.engine.config()
        gallery = PresentationGallery(
            identity, config["engine"], config["version"], dict(config),
            self.policy)
        for presentation, frames in enumerate(touch_groups):
            if len(gallery.entries) >= self.policy.max_records_per_identity:
                break
            self._add_presentation(gallery, presentation, frames)
            gallery.presentations += 1
        return gallery

    def _add_presentation(self, gallery, presentation, frames):
        tracker = TouchTracker(self.frame_spec, features=self.features)
        for sequence, raw in enumerate(frames, 1):
            tracker.observe(raw, sequence)
        grouped = {}
        for item in tracker.observations:
            if item.quality >= self.policy.min_quality:
                grouped.setdefault(item.registration.component, []).append(item)
        for values in grouped.values():
            values.sort(key=lambda item: item.quality, reverse=True)
        selected = []
        component_reserve = (min(4, len(grouped))
                             if self.policy.include_component_images else 0)
        frame_limit = max(1, self.policy.max_records_per_presentation - component_reserve)
        positions = {component: 0 for component in grouped}
        while len(selected) < frame_limit:
            progress = False
            for component in sorted(grouped):
                values = grouped[component]
                while positions[component] < len(values):
                    item = values[positions[component]]
                    positions[component] += 1
                    if any(ssim(item.image, old.image, data_range=255) >=
                           self.policy.duplicate_ssim for old in selected):
                        continue
                    selected.append(item)
                    progress = True
                    break
                if len(selected) >= frame_limit:
                    break
            if not progress:
                break
        for item in selected:
            image = FingerprintImage.from_frame(
                self.features.raw_frame_to_image(item.raw_frame),
                self.policy.frame_border)
            self._extract(gallery, presentation, item.registration.component,
                          item.sequence, "frame", item.quality, image)
        if self.policy.include_component_images:
            stitcher = TouchStitcher(
                self.frame_spec, tracker=tracker, blend="winner")
            snapshots = sorted(
                stitcher.component_snapshots(),
                key=lambda item: (item.admitted_frames, item.coverage_pixels),
                reverse=True)
            for stitched in snapshots:
                if stitched.admitted_frames < 2:
                    continue
                image = FingerprintImage(stitched.image, stitched.mask)
                quality = min(1.0, stitched.coverage_pixels /
                              (self.frame_spec.byte_count * 2.0))
                self._extract(gallery, presentation, stitched.component, 0,
                              "component", quality, image)

    def _extract(self, gallery, presentation, component, sequence,
                 representation, quality, image):
        if (len(gallery.entries) >= gallery.policy.max_records_per_identity or
                sum(entry.presentation == presentation for entry in gallery.entries) >=
                gallery.policy.max_records_per_presentation):
            return
        record, reason, _ = self.engine.extract_record(image)
        if record is None:
            gallery.extraction_failures += 1
            return
        gallery.add(GalleryEntry(
            presentation, component, sequence, representation,
            float(quality), record))


@dataclass(frozen=True)
class GalleryDecision:
    identity: str | None
    accepted: bool
    reason: str
    score: float
    margin: float
    metrics: dict


class StreamingGalleryMatcher:
    """Extractor-owned identity scores; shadow-only until separately calibrated."""

    def __init__(self, engine, galleries, frame_spec, border=4):
        if not galleries:
            raise ValueError("streaming gallery matcher requires galleries")
        self.engine = engine
        self.galleries = dict(galleries)
        config = engine.config()
        if any(gallery.engine_config != config
               for gallery in self.galleries.values()):
            raise ValueError("gallery engine configuration mismatch")
        self.frame_spec = frame_spec
        self.border = border
        self.frames_seen = 0
        self.extraction_failures = 0
        self._trajectories = {}

    def begin_touch(self):
        self.frames_seen = 0
        self.extraction_failures = 0
        self._trajectories = {identity: [] for identity in self.galleries}

    def discontinuity(self):
        self.begin_touch()

    def observe(self, raw_frame, sequence=None):
        self.frames_seen += 1
        if len(raw_frame) != self.frame_spec.byte_count:
            raise ValueError("invalid gallery probe frame")
        pixels = np.frombuffer(raw_frame, dtype=np.uint8).reshape(
            self.frame_spec.height, self.frame_spec.width)
        record, reason, elapsed = self.engine.extract_record(
            FingerprintImage.from_frame(pixels, self.border))
        if record is None:
            self.extraction_failures += 1
            return self._decision({}, reason, elapsed)
        scores = {}
        comparison_started = time.perf_counter()
        try:
            for identity, gallery in self.galleries.items():
                values = [self.engine.compare_records(record, entry.record)
                          for entry in gallery.entries]
                score = max(values) if values else 0.0
                scores[identity] = float(score)
                self._trajectories.setdefault(identity, []).append(float(score))
        except (OSError, RuntimeError, TypeError, ValueError):
            comparison_ms = (time.perf_counter() - comparison_started) * 1000.0
            return self._decision({}, "engine_error", elapsed, comparison_ms)
        comparison_ms = (time.perf_counter() - comparison_started) * 1000.0
        return self._decision(
            scores, "shadow_uncalibrated", elapsed, comparison_ms)

    def _decision(self, scores, reason, elapsed, comparison_ms=0.0):
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        best_identity, score = ranked[0] if ranked else (None, 0.0)
        runner = ranked[1] if len(ranked) > 1 else (None, 0.0)
        metrics = {
            "best_identity": best_identity,
            "best": {
                "score": score,
                "frames_seen": self.frames_seen,
                "extraction_failures": self.extraction_failures,
            },
            "runner_up": runner[0],
            "runner_up_score": runner[1],
            "scores": scores,
            "extraction_ms": float(elapsed),
            "comparison_ms": float(comparison_ms),
        }
        return GalleryDecision(None, False, reason, float(score),
                               float(score - runner[1]), metrics)
