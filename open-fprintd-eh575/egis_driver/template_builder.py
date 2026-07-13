import logging
import time

import numpy as np
from skimage.metrics import structural_similarity as ssim

from egis_driver.image_features import ImageFeatureExtractor

logger = logging.getLogger("TEMPLATE")


class TemplateBuilder:
    """Enrollment touch analysis and template payload construction."""

    def __init__(
            self,
            features=None,
            min_touch_contrast=12.0,
            min_touch_keypoints=4):
        self.features = features or ImageFeatureExtractor()
        self.min_touch_contrast = min_touch_contrast
        self.min_touch_keypoints = min_touch_keypoints

    def analyze_touch(self, raw_frames):
        stats = {
            "frames": len(raw_frames),
            "usable_frames": 0,
            "contrast_min": 0.0,
            "contrast_avg": 0.0,
            "contrast_max": 0.0,
            "quality_avg": 0.0,
            "max_keypoints": 0,
            "usable": False,
        }

        contrasts = []
        qualities = []
        for raw in raw_frames:
            try:
                raw_img = self.features.raw_frame_to_image(raw)
            except Exception:
                continue

            contrast = float(np.std(raw_img))
            preprocessed = self.features.preprocess(raw_img)
            quality = float(self.features.frame_quality(raw_img))
            kp, _ = self.features.detect_features(preprocessed)
            keypoints = len(kp) if kp is not None else 0

            contrasts.append(contrast)
            qualities.append(quality)
            stats["max_keypoints"] = max(stats["max_keypoints"], keypoints)
            if keypoints >= self.min_touch_keypoints and contrast >= self.min_touch_contrast:
                stats["usable_frames"] += 1

        if contrasts:
            stats["contrast_min"] = min(contrasts)
            stats["contrast_avg"] = sum(contrasts) / len(contrasts)
            stats["contrast_max"] = max(contrasts)
        if qualities:
            stats["quality_avg"] = sum(qualities) / len(qualities)

        stats["usable"] = (
            stats["usable_frames"] >= 1 and
            stats["max_keypoints"] >= self.min_touch_keypoints
        )
        return stats

    def build_payload(self, name, raw_frames, schema_version, matcher_version):
        touch_groups = self._normalize_touch_groups(raw_frames)
        frame_count = sum(len(group) for group in touch_groups)
        logger.info(
            "Building enrollment template for %s (%d touches, %d frames)...",
            name,
            len(touch_groups),
            frame_count,
        )

        scored_groups = []
        for group in touch_groups:
            scored_frames = []
            for raw in group:
                img_arr = self.features.raw_frame_to_image(raw)
                quality = self.features.frame_quality(img_arr)
                img = self.features.preprocess(img_arr)
                scored_frames.append((img, quality))
            if scored_frames:
                scored_frames.sort(key=lambda item: item[1], reverse=True)
                cutoff = max(1, len(scored_frames) // 2)
                scored_groups.append(scored_frames[:cutoff])

        if not scored_groups:
            return None, None, 0

        good_frame_count = sum(len(group) for group in scored_groups)
        diverse_frames = self._remove_duplicates_by_touch(
            scored_groups,
            max_frames=40,
            ssim_threshold=0.95,
        )

        logger.info("Quality filter: kept %d/%d", good_frame_count, frame_count)
        logger.info(
            "Touch-balanced dedup filter: kept %d templates from %d touches",
            len(diverse_frames),
            len(scored_groups),
        )

        new_templates = []
        for img, quality in diverse_frames:
            kp, des = self.features.detect_features(img)
            if des is not None and len(kp) > 3:
                packed_kp = [(p.pt, p.size, p.angle, p.response, p.octave, p.class_id) for p in kp]
                new_templates.append({
                    "keypoints": packed_kp,
                    "descriptors": des,
                    "image": img,
                    "ridge": self.features.template_descriptor(img),
                    "quality": float(quality),
                })

        if not new_templates:
            return None, None, 0

        save_data = {"num_templates": np.array(len(new_templates), dtype=np.int32)}
        for i, template in enumerate(new_templates):
            kp_arr = np.array([
                [p[0][0], p[0][1], p[1], p[2], p[3], p[4], p[5]]
                for p in template["keypoints"]
            ], dtype=np.float32)
            ridge = template["ridge"]["orientation"]
            save_data[f"kp_{i}"] = kp_arr
            save_data[f"desc_{i}"] = template["descriptors"].astype(np.float32)
            save_data[f"img_{i}"] = template["image"].astype(np.uint8)
            save_data[f"ridge_cos2_{i}"] = ridge["cos2"].astype(np.float32)
            save_data[f"ridge_sin2_{i}"] = ridge["sin2"].astype(np.float32)
            save_data[f"ridge_weight_{i}"] = ridge["weight"].astype(np.float32)
            save_data[f"quality_{i}"] = np.float32(template["quality"])

        meta = {
            "schema_version": schema_version,
            "matcher_version": matcher_version,
            "name": name,
            "created_at": int(time.time()),
            "enrollment_touches": len(touch_groups),
        }

        return save_data, meta, len(new_templates)

    def _normalize_touch_groups(self, raw_frames):
        if not raw_frames:
            return []

        first = raw_frames[0]
        flat_frames = isinstance(first, (bytes, bytearray, memoryview, np.ndarray))
        if (
                isinstance(first, (list, tuple)) and
                first and
                isinstance(first[0], (int, float, np.integer, np.floating))):
            flat_frames = True

        if flat_frames:
            return [list(raw_frames)]
        return [list(group) for group in raw_frames if group]

    def _remove_duplicates_by_touch(
            self,
            groups_with_quality,
            max_frames=40,
            ssim_threshold=0.95):
        groups = [
            sorted(group, key=lambda item: item[1], reverse=True)
            for group in groups_with_quality
            if group
        ]
        positions = [0] * len(groups)
        unique = []

        while len(unique) < max_frames:
            made_progress = False
            for group_index, group in enumerate(groups):
                while positions[group_index] < len(group):
                    frame, quality = group[positions[group_index]]
                    positions[group_index] += 1
                    if self._is_duplicate(
                            frame,
                            unique,
                            ssim_threshold=ssim_threshold):
                        continue
                    unique.append((frame, quality))
                    made_progress = True
                    break
                if len(unique) >= max_frames:
                    break
            if not made_progress:
                break
        return unique

    def _is_duplicate(self, frame, selected, ssim_threshold):
        for existing, _ in selected:
            try:
                score = ssim(frame, existing, data_range=255)
            except Exception:
                score = 0.0
            if score > ssim_threshold:
                return True
        return False

    def _remove_duplicates(self, frames_with_quality, max_frames=40, ssim_threshold=0.95):
        frames_with_quality.sort(key=lambda x: x[1], reverse=True)

        unique = []
        for frame, quality in frames_with_quality:
            if not self._is_duplicate(frame, unique, ssim_threshold):
                unique.append((frame, quality))
            if len(unique) >= max_frames:
                break
        return unique
