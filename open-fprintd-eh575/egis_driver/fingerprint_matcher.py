import cv2
import json
import numpy as np
import os
import time
from skimage.metrics import structural_similarity as ssim

MATCHER_VERSION = 3
TEMPLATE_SCHEMA_VERSION = 4
CALIBRATION_DIR = "/var/lib/open-fprintd/egis-calibration"
THRESHOLD_FILE = os.path.join(CALIBRATION_DIR, "thresholds.json")
THRESHOLD_KEYS = (
    "min_inliers",
    "min_inlier_ratio",
    "min_inlier_frames",
    "min_frame_inliers",
    "min_margin",
    "min_ncc",
    "min_orientation",
    "min_ridge_score",
)

class FingerprintMatcher:
    def __init__(self, enroll_dir="/var/lib/open-fprintd/egis"):
        self.enroll_dir = enroll_dir
        if not os.path.exists(enroll_dir):
            try:
                os.makedirs(enroll_dir)
            except PermissionError:
                print(f"[MATCHER] ERROR: Cannot create {enroll_dir}. Run as root.")

        self.sift = cv2.SIFT_create(
            nfeatures=0,
            nOctaveLayers=3,
            contrastThreshold=0.04,
            edgeThreshold=10,
            sigma=1.2,
        )

        self.UPSCALE_FACTOR = 2

        index_params = dict(algorithm=1, trees=5)
        search_params = dict(checks=50)
        self.flann = cv2.FlannBasedMatcher(index_params, search_params)

        self.descriptor_map = []
        self.descriptor_lookup = {}
        self.cached_templates = {}
        self.train_descriptors = None
        self.last_verify_stats = {}
        self.min_enroll_touch_contrast = 12.0
        self.min_enroll_touch_keypoints = 4
        self.min_verify_inliers = 40
        self.min_verify_inlier_ratio = 0.72
        self.min_verify_inlier_frames = 1
        self.min_verify_frame_inliers = 10
        self.min_verify_margin = 25.0
        self.min_verify_ncc = 0.30
        self.min_verify_orientation = 0.35
        self.min_verify_ridge_score = 0.38
        self.calibrated = False
        self.threshold_source = None
        self.legacy_templates = []
        self.thresholds_by_target = {}
        self._load_thresholds()

        self.rebuild_index()

    def _load_thresholds(self):
        try:
            with open(THRESHOLD_FILE, "r") as f:
                data = json.load(f)
            if data.get("matcher_version") != MATCHER_VERSION:
                print("[MATCHER] Calibration version mismatch; verification will fail closed.")
                return False
            if data.get("validated") is not True:
                print("[MATCHER] Calibration thresholds are not validated; verification will fail closed.")
                return False
            thresholds = self._coerce_thresholds(data.get("thresholds", {}))
            self._set_default_thresholds(thresholds)
            target_thresholds = data.get("thresholds_by_target", {})
            self.thresholds_by_target = {
                target: self._coerce_thresholds(values)
                for target, values in target_thresholds.items()
            }
            self.calibrated = True
            self.threshold_source = THRESHOLD_FILE
            print(f"[MATCHER] Loaded calibrated thresholds from {THRESHOLD_FILE}")
            return True
        except FileNotFoundError:
            print("[MATCHER] No calibration thresholds found; verification will fail closed.")
        except Exception as e:
            print(f"[MATCHER] Failed to load calibration thresholds: {e}; verification will fail closed.")
        return False

    def _coerce_thresholds(self, thresholds):
        missing = [key for key in THRESHOLD_KEYS if key not in thresholds]
        if missing:
            raise KeyError(f"missing threshold keys: {', '.join(missing)}")
        return {
            "min_inliers": int(thresholds["min_inliers"]),
            "min_inlier_ratio": float(thresholds["min_inlier_ratio"]),
            "min_inlier_frames": int(thresholds["min_inlier_frames"]),
            "min_frame_inliers": int(thresholds["min_frame_inliers"]),
            "min_margin": float(thresholds["min_margin"]),
            "min_ncc": float(thresholds["min_ncc"]),
            "min_orientation": float(thresholds["min_orientation"]),
            "min_ridge_score": float(thresholds["min_ridge_score"]),
        }

    def _default_thresholds(self):
        return {
            "min_inliers": self.min_verify_inliers,
            "min_inlier_ratio": self.min_verify_inlier_ratio,
            "min_inlier_frames": self.min_verify_inlier_frames,
            "min_frame_inliers": self.min_verify_frame_inliers,
            "min_margin": self.min_verify_margin,
            "min_ncc": self.min_verify_ncc,
            "min_orientation": self.min_verify_orientation,
            "min_ridge_score": self.min_verify_ridge_score,
        }

    def _set_default_thresholds(self, thresholds):
        self.min_verify_inliers = thresholds["min_inliers"]
        self.min_verify_inlier_ratio = thresholds["min_inlier_ratio"]
        self.min_verify_inlier_frames = thresholds["min_inlier_frames"]
        self.min_verify_frame_inliers = thresholds["min_frame_inliers"]
        self.min_verify_margin = thresholds["min_margin"]
        self.min_verify_ncc = thresholds["min_ncc"]
        self.min_verify_orientation = thresholds["min_orientation"]
        self.min_verify_ridge_score = thresholds["min_ridge_score"]

    def _active_thresholds(self, username=None, finger_name=None):
        if username and finger_name:
            key = f"{username}/{finger_name}"
            if key in self.thresholds_by_target:
                return self.thresholds_by_target[key]
        return self._default_thresholds()

    def _normalize_verify_finger(self, finger_name):
        if finger_name is None:
            return None
        normalized = str(finger_name).strip()
        if not normalized or normalized == "any":
            return None
        return normalized

    def _raw_frame_to_image(self, raw):
        return np.array(list(raw), dtype=np.uint8).reshape((52, 103))

    def _preprocess(self, img_array):
        img = cv2.normalize(img_array, None, 0, 255, cv2.NORM_MINMAX).astype('uint8')
        img = cv2.equalizeHist(img)
        img = cv2.GaussianBlur(img, (3, 3), 0)
        return img

    def _detect_features(self, img):
        h, w = img.shape[:2]
        upscaled = cv2.resize(img, (w * self.UPSCALE_FACTOR, h * self.UPSCALE_FACTOR),
                              interpolation=cv2.INTER_CUBIC)
        kp, des = self.sift.detectAndCompute(upscaled, None)
        if kp is not None:
            scale = 1.0 / self.UPSCALE_FACTOR
            for k in kp:
                k.pt = (k.pt[0] * scale, k.pt[1] * scale)
                k.size = k.size * scale
        return kp, des

    def _frame_quality(self, img):
        img_f = img.astype(np.float32)

        contrast = np.clip(float(np.std(img_f)) / 128.0, 0, 1)

        laplacian = cv2.Laplacian(img, cv2.CV_64F)
        sharpness = np.clip(laplacian.var() / 5000.0, 0, 1)

        fg_mask = (img > 20) & (img < 240)
        foreground = float(np.sum(fg_mask)) / float(img.size)

        gy, gx = np.gradient(img_f)
        clarity = np.clip(float(np.mean(np.sqrt(gx**2 + gy**2))) / 80.0, 0, 1)

        return contrast * 0.2 + sharpness * 0.3 + foreground * 0.2 + clarity * 0.3

    def _orientation_descriptor(self, img, block_size=8):
        img_f = img.astype(np.float32)
        gx = cv2.Sobel(img_f, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(img_f, cv2.CV_32F, 0, 1, ksize=3)

        rows = []
        weights = []
        for y in range(0, img.shape[0] - block_size + 1, block_size):
            row = []
            weight_row = []
            for x in range(0, img.shape[1] - block_size + 1, block_size):
                bx = gx[y:y + block_size, x:x + block_size]
                by = gy[y:y + block_size, x:x + block_size]
                gxx = float(np.sum(bx * bx))
                gyy = float(np.sum(by * by))
                gxy = float(np.sum(bx * by))
                denom = gxx + gyy + 1e-6
                coh_x = gxx - gyy
                coh_y = 2.0 * gxy
                reliability = float(np.sqrt(coh_x * coh_x + coh_y * coh_y) / denom)
                angle = 0.5 * np.arctan2(coh_y, coh_x)
                row.append((float(np.cos(2.0 * angle)), float(np.sin(2.0 * angle))))
                weight_row.append(float(np.clip(reliability, 0.0, 1.0)))
            if row:
                rows.append(row)
                weights.append(weight_row)

        if not rows:
            return {
                "cos2": np.zeros((0, 0), dtype=np.float32),
                "sin2": np.zeros((0, 0), dtype=np.float32),
                "weight": np.zeros((0, 0), dtype=np.float32),
            }

        vectors = np.array(rows, dtype=np.float32)
        return {
            "cos2": vectors[:, :, 0],
            "sin2": vectors[:, :, 1],
            "weight": np.array(weights, dtype=np.float32),
        }

    def _template_descriptor(self, img):
        return {
            "orientation": self._orientation_descriptor(img),
        }

    def _normalized_correlation(self, a, b, mask):
        valid = mask > 0
        if int(np.sum(valid)) < 128:
            return 0.0

        av = a[valid].astype(np.float32)
        bv = b[valid].astype(np.float32)
        av = av - float(np.mean(av))
        bv = bv - float(np.mean(bv))
        denom = float(np.sqrt(np.sum(av * av) * np.sum(bv * bv))) + 1e-6
        return float(np.clip(np.sum(av * bv) / denom, -1.0, 1.0))

    def _orientation_similarity(self, live_img, template_ridge, mask):
        live = self._orientation_descriptor(live_img)
        stored = template_ridge.get("orientation", {})
        if live["cos2"].shape != stored.get("cos2", np.zeros((0, 0))).shape:
            return 0.0

        block_size = 8
        h_blocks, w_blocks = live["cos2"].shape
        if h_blocks == 0 or w_blocks == 0:
            return 0.0

        mask_blocks = cv2.resize(
            mask.astype(np.float32),
            (w_blocks, h_blocks),
            interpolation=cv2.INTER_AREA,
        )
        weights = live["weight"] * stored["weight"] * np.clip(mask_blocks, 0.0, 1.0)
        total_weight = float(np.sum(weights))
        if total_weight < 0.5:
            return 0.0

        similarity = live["cos2"] * stored["cos2"] + live["sin2"] * stored["sin2"]
        return float(np.clip(np.sum(similarity * weights) / total_weight, -1.0, 1.0))

    def _ridge_consistency(self, live_img, template, homography):
        stored_img = template["image"]
        h, w = stored_img.shape[:2]
        aligned = cv2.warpPerspective(
            live_img,
            homography,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        valid_mask = cv2.warpPerspective(
            np.ones(live_img.shape, dtype=np.uint8),
            homography,
            (w, h),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )

        coverage = float(np.sum(valid_mask > 0)) / float(valid_mask.size)
        if coverage < 0.35:
            return {
                "ncc": 0.0,
                "edge_ncc": 0.0,
                "orientation": 0.0,
                "ridge_score": 0.0,
                "coverage": coverage,
            }

        ncc = self._normalized_correlation(aligned, stored_img, valid_mask)
        aligned_edges = cv2.Sobel(aligned, cv2.CV_32F, 1, 0, ksize=3)
        stored_edges = cv2.Sobel(stored_img, cv2.CV_32F, 1, 0, ksize=3)
        edge_ncc = self._normalized_correlation(aligned_edges, stored_edges, valid_mask)
        orientation = self._orientation_similarity(aligned, template["ridge"], valid_mask)
        ridge_score = (
            max(0.0, ncc) * 0.40 +
            max(0.0, edge_ncc) * 0.20 +
            max(0.0, orientation) * 0.40
        )

        return {
            "ncc": float(ncc),
            "edge_ncc": float(edge_ncc),
            "orientation": float(orientation),
            "ridge_score": float(ridge_score),
            "coverage": float(coverage),
        }

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
                raw_img = self._raw_frame_to_image(raw)
            except Exception:
                continue

            contrast = float(np.std(raw_img))
            preprocessed = self._preprocess(raw_img)
            quality = float(self._frame_quality(raw_img))
            kp, _ = self._detect_features(preprocessed)
            keypoints = len(kp) if kp is not None else 0

            contrasts.append(contrast)
            qualities.append(quality)
            stats["max_keypoints"] = max(stats["max_keypoints"], keypoints)
            if keypoints >= self.min_enroll_touch_keypoints and contrast >= self.min_enroll_touch_contrast:
                stats["usable_frames"] += 1

        if contrasts:
            stats["contrast_min"] = min(contrasts)
            stats["contrast_avg"] = sum(contrasts) / len(contrasts)
            stats["contrast_max"] = max(contrasts)
        if qualities:
            stats["quality_avg"] = sum(qualities) / len(qualities)

        stats["usable"] = (
            stats["usable_frames"] >= 1 and
            stats["max_keypoints"] >= self.min_enroll_touch_keypoints
        )
        return stats

    def _remove_duplicates(self, frames_with_quality, max_frames=40, ssim_threshold=0.95):
        frames_with_quality.sort(key=lambda x: x[1], reverse=True)

        unique = []
        for frame, quality in frames_with_quality:
            is_duplicate = False
            for existing, _ in unique:
                try:
                    score = ssim(frame, existing, data_range=255)
                except Exception:
                    score = 0.0
                if score > ssim_threshold:
                    is_duplicate = True
                    break
            if not is_duplicate:
                unique.append((frame, quality))
            if len(unique) >= max_frames:
                break
        return unique

    def rebuild_index(self):
        print("[MATCHER] Rebuilding global FLANN Index...")
        start_t = time.time()

        all_descriptors = []
        self.descriptor_map = []
        self.descriptor_lookup = {}
        self.cached_templates = {}
        self.legacy_templates = []

        current_idx_offset = 0

        for filename in sorted(os.listdir(self.enroll_dir)):
            if filename.endswith(".npy"):
                self.legacy_templates.append(filename)
                continue

            if not filename.endswith(".npz"):
                continue

            safe_name = filename[:-4]
            json_path = os.path.join(self.enroll_dir, safe_name + ".json")
            try:
                with open(json_path, "r") as f:
                    meta = json.load(f)
            except Exception as e:
                print(f"[MATCHER] Missing or corrupt metadata for {filename}: {e}")
                continue

            if meta.get("schema_version") != TEMPLATE_SCHEMA_VERSION:
                self.legacy_templates.append(filename)
                print(f"[MATCHER] Ignoring incompatible template {filename}; re-enrollment required.")
                continue

            try:
                npz = np.load(os.path.join(self.enroll_dir, filename), allow_pickle=False)
            except Exception as e:
                print(f"[MATCHER] Failed to load {filename}: {e}")
                continue

            try:
                num_templates = int(npz["num_templates"])
            except KeyError:
                print(f"[MATCHER] Corrupt template {filename}: missing num_templates")
                continue

            unpacked_templates = []
            for t_idx in range(num_templates):
                try:
                    kp_data = npz[f"kp_{t_idx}"]
                    des = npz[f"desc_{t_idx}"]
                    image = npz[f"img_{t_idx}"]
                    ridge_cos2 = npz[f"ridge_cos2_{t_idx}"]
                    ridge_sin2 = npz[f"ridge_sin2_{t_idx}"]
                    ridge_weight = npz[f"ridge_weight_{t_idx}"]
                    quality = float(npz[f"quality_{t_idx}"])
                except KeyError:
                    continue

                if des is None or len(des) < 2:
                    continue

                kp = [cv2.KeyPoint(
                    x=float(row[0]), y=float(row[1]),
                    size=float(row[2]), angle=float(row[3]),
                    response=float(row[4]), octave=int(row[5]),
                    class_id=int(row[6]))
                    for row in kp_data]

                ridge = {
                    "orientation": {
                        "cos2": ridge_cos2,
                        "sin2": ridge_sin2,
                        "weight": ridge_weight,
                    }
                }

                unpacked_templates.append({
                    "keypoints": kp,
                    "descriptors": des,
                    "image": image.astype(np.uint8),
                    "ridge": ridge,
                    "quality": quality,
                })

                all_descriptors.append(des)

                num_des = len(des)
                self.descriptor_map.append({
                    "start": current_idx_offset,
                    "end": current_idx_offset + num_des,
                    "file": filename,
                    "idx": t_idx
                })

                for gi in range(current_idx_offset, current_idx_offset + num_des):
                    self.descriptor_lookup[gi] = (
                        filename, t_idx, gi - current_idx_offset
                    )

                current_idx_offset += num_des

            if unpacked_templates:
                self.cached_templates[filename] = unpacked_templates

        if all_descriptors:
            self.train_descriptors = np.vstack(all_descriptors)
            self.flann.clear()
            self.flann.add([self.train_descriptors])
            self.flann.train()
            print(f"[MATCHER] Index built in {time.time()-start_t:.2f}s. Total Features: {current_idx_offset}")
        else:
            print("[MATCHER] Index is empty (no enrolled prints).")
            if self.legacy_templates:
                print(f"[MATCHER] {len(self.legacy_templates)} legacy template file(s) require re-enrollment.")
            self.train_descriptors = None

    def enroll_finger(self, name, raw_frames):
        print(f"[MATCHER] Enrolling {name} ({len(raw_frames)} frames)...")

        scored_frames = []
        for raw in raw_frames:
            img_arr = self._raw_frame_to_image(raw)
            quality = self._frame_quality(img_arr)
            img = self._preprocess(img_arr)
            scored_frames.append((img, quality))

        if not scored_frames:
            return False

        scored_frames.sort(key=lambda x: x[1], reverse=True)
        cutoff = max(1, len(scored_frames) // 2)
        good_frames = scored_frames[:cutoff]

        diverse_frames = self._remove_duplicates(good_frames, max_frames=40, ssim_threshold=0.95)

        print(f"[MATCHER] Quality filter: kept {len(good_frames)}/{len(scored_frames)}")
        print(f"[MATCHER] Dedup filter: kept {len(diverse_frames)} templates")

        new_templates = []
        for img, quality in diverse_frames:
            kp, des = self._detect_features(img)
            if des is not None and len(kp) > 3:
                packed_kp = [(p.pt, p.size, p.angle, p.response, p.octave, p.class_id) for p in kp]
                new_templates.append({
                    "keypoints": packed_kp,
                    "descriptors": des,
                    "image": img,
                    "ridge": self._template_descriptor(img),
                    "quality": float(quality),
                })

        if not new_templates:
            return False

        safe_name = name.replace("/", "_")
        base_path = os.path.join(self.enroll_dir, safe_name)
        json_path = base_path + ".json"
        npz_path = base_path + ".npz"

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

        np.savez(npz_path, **save_data)
        os.chmod(npz_path, 0o600)

        meta = {
            "schema_version": TEMPLATE_SCHEMA_VERSION,
            "matcher_version": MATCHER_VERSION,
            "name": name,
            "created_at": int(time.time()),
        }
        with open(json_path, "w") as f:
            json.dump(meta, f)
        os.chmod(json_path, 0o600)

        print(f"[MATCHER] Saved {len(new_templates)} templates for {name}")
        self.rebuild_index()
        return True

    def verify_finger(self, raw_frame):
        """Single-frame verification (legacy)."""
        return self.verify_finger_multiframe([raw_frame])

    def verify_finger_multiframe(self, raw_frames, username=None, finger_name=None, apply_thresholds=True):
        """
        Multi-frame verification.
        SIFT/FLANN proposes candidate alignments. Authentication then requires
        per-frame geometric and ridge/image consistency against the exact target.

        Args:
            raw_frames: List of raw frame data (each is bytes or list of ints)
            username: Optional fprintd username. If set, only that user's templates are considered.
            finger_name: Optional fprintd finger name. If set, only that exact finger is considered.

        Returns:
            (username, score) tuple or (None, 0) if no match
        """
        finger_name = self._normalize_verify_finger(finger_name)

        if self.train_descriptors is None:
            self.last_verify_stats = {
                "frames": len(raw_frames),
                "keypoints": 0,
                "good_matches": 0,
                "candidates": 0,
                "best_inliers": 0,
                "calibrated": self.calibrated,
                "reject_reason": "no_templates",
                "legacy_templates": list(self.legacy_templates),
            }
            return None, 0

        all_keypoints = []
        all_descriptors_list = []
        query_frame_ids = []
        query_images = []

        for frame_idx, raw_frame in enumerate(raw_frames):
            img_arr = self._raw_frame_to_image(raw_frame)
            img = self._preprocess(img_arr)
            query_images.append(img)
            kp, des = self._detect_features(img)

            if des is not None and len(kp) >= 4:
                all_keypoints.extend(kp)
                all_descriptors_list.append(des)
                query_frame_ids.extend([frame_idx] * len(des))

        if not all_descriptors_list or len(all_keypoints) < 4:
            print(f"[MATCHER] Too few keypoints across {len(raw_frames)} frames")
            self.last_verify_stats = {
                "frames": len(raw_frames),
                "keypoints": len(all_keypoints),
                "good_matches": 0,
                "candidates": 0,
                "best_inliers": 0,
                "calibrated": self.calibrated,
                "reject_reason": "too_few_keypoints",
            }
            return None, 0

        des_live = np.vstack(all_descriptors_list)

        print(f"[MATCHER] Combined {len(all_keypoints)} keypoints from {len(raw_frames)} frames")

        matches = self.flann.knnMatch(des_live, k=2)

        good_matches = []
        for pair in matches:
            if len(pair) < 2:
                continue
            m, n = pair
            if m.distance < 0.75 * n.distance:
                good_matches.append(m)

        print(f"[MATCHER] Good matches after ratio test: {len(good_matches)}")

        if len(good_matches) < 4:
            self.last_verify_stats = {
                "frames": len(raw_frames),
                "keypoints": len(all_keypoints),
                "good_matches": len(good_matches),
                "candidates": 0,
                "best_inliers": 0,
                "calibrated": self.calibrated,
                "reject_reason": "sift_weak",
            }
            return None, 0

        candidate_votes = {}
        user_prefix = f"{username}_" if username else None
        target_filename = f"{username}_{finger_name}.npz" if username and finger_name else None

        for m in good_matches:
            global_idx = m.trainIdx

            owner = self.descriptor_lookup.get(global_idx)
            if owner is None:
                continue

            filename, t_idx, local_idx = owner
            if user_prefix and not filename.startswith(user_prefix):
                continue
            if user_prefix and "_" in filename[len(user_prefix):].rsplit(".", 1)[0]:
                continue

            key = (filename, t_idx)
            if key not in candidate_votes:
                candidate_votes[key] = []

            new_m = cv2.DMatch(m.queryIdx, local_idx, m.imgIdx, m.distance)
            candidate_votes[key].append(new_m)

        if not candidate_votes:
            self.last_verify_stats = {
                "frames": len(raw_frames),
                "keypoints": len(all_keypoints),
                "good_matches": len(good_matches),
                "candidates": 0,
                "best_inliers": 0,
                "calibrated": self.calibrated,
                "reject_reason": "no_target_candidate",
            }
            return None, 0

        print(f"[MATCHER] Candidates: {len(candidate_votes)}")

        sorted_candidates = sorted(candidate_votes.items(), key=lambda item: len(item[1]), reverse=True)
        top_candidates = sorted_candidates[:5]
        thresholds = self._active_thresholds(username, finger_name)

        best_result = (None, 0)
        best_score = 0.0
        best_inliers = 0
        best_metrics = {}
        candidate_records = []

        for i, (candidate_key, candidate_matches) in enumerate(top_candidates):
            if len(candidate_matches) < 4:
                continue

            filename, t_idx = candidate_key
            if filename not in self.cached_templates:
                continue
            template = self.cached_templates[filename][t_idx]
            kp_stored = template["keypoints"]

            frame_records = []
            frame_inliers = {}
            all_inlier_dists = []
            valid_votes = 0

            for frame_id in sorted(set(query_frame_ids[m.queryIdx] for m in candidate_matches)):
                frame_matches = [
                    m for m in candidate_matches
                    if query_frame_ids[m.queryIdx] == frame_id
                ]
                valid_votes += len(frame_matches)
                if len(frame_matches) < 4:
                    continue

                src_pts = np.float32([all_keypoints[m.queryIdx].pt for m in frame_matches]).reshape(-1, 1, 2)
                dst_pts = np.float32([kp_stored[m.trainIdx].pt for m in frame_matches]).reshape(-1, 1, 2)

                M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 8.0)
                if M is None or mask is None:
                    continue

                inliers = int(np.sum(mask))
                if inliers < 4:
                    continue

                sx = np.sqrt(M[0, 0] ** 2 + M[1, 0] ** 2)
                sy = np.sqrt(M[0, 1] ** 2 + M[1, 1] ** 2)
                angle = abs(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
                perspective_warp = abs(M[2, 0]) + abs(M[2, 1])

                homography_valid = (
                    0.45 < sx < 2.2 and
                    0.45 < sy < 2.2 and
                    angle < 55 and
                    perspective_warp < 0.03
                )
                if not homography_valid:
                    continue

                ridge = self._ridge_consistency(query_images[frame_id], template, M)
                inlier_dists = [m.distance for m, inc in zip(frame_matches, mask.ravel()) if inc]
                all_inlier_dists.extend(inlier_dists)
                frame_inliers[frame_id] = inliers
                frame_records.append({
                    "frame": int(frame_id),
                    "votes": int(len(frame_matches)),
                    "inliers": int(inliers),
                    "inlier_ratio": float(inliers / len(frame_matches)),
                    "scale_x": float(sx),
                    "scale_y": float(sy),
                    "angle": float(angle),
                    "warp": float(perspective_warp),
                    "ncc": ridge["ncc"],
                    "edge_ncc": ridge["edge_ncc"],
                    "orientation": ridge["orientation"],
                    "ridge_score": ridge["ridge_score"],
                    "coverage": ridge["coverage"],
                })

            if not frame_records:
                print(f"[MATCHER] Candidate {i+1} ({filename}): votes={len(candidate_matches)}, no valid frame alignments")
                continue

            inliers = sum(record["inliers"] for record in frame_records)
            inlier_ratio = inliers / max(1, valid_votes)
            inlier_frame_count = len(frame_records)
            strong_frame_count = sum(
                1 for record in frame_records
                if (
                    record["inliers"] >= thresholds["min_frame_inliers"] and
                    record["ridge_score"] >= thresholds["min_ridge_score"]
                )
            )
            avg_dist = np.mean(all_inlier_dists) if all_inlier_dists else 999.0
            dist_score = max(0, 1.0 - avg_dist / 400.0)
            total_weight = max(1, sum(record["inliers"] for record in frame_records))
            ridge_score = sum(record["ridge_score"] * record["inliers"] for record in frame_records) / total_weight
            ncc = sum(record["ncc"] * record["inliers"] for record in frame_records) / total_weight
            orientation = sum(record["orientation"] * record["inliers"] for record in frame_records) / total_weight

            diagnostic_score = inliers * inlier_ratio * (1.0 + dist_score) * max(0.0, ridge_score)

            print(f"[MATCHER]   -> diagnostic_score={diagnostic_score:.2f} "
                  f"(inliers={inliers}, ratio={inlier_ratio:.2f}, frames={inlier_frame_count}, "
                  f"strong_frames={strong_frame_count}, frame_inliers={frame_inliers}, "
                  f"dist_score={dist_score:.2f}, ncc={ncc:.2f}, orientation={orientation:.2f}, "
                  f"ridge_score={ridge_score:.2f})")

            best_inliers = max(best_inliers, inliers)
            candidate_metrics = {
                "filename": filename,
                "name": filename.replace(".npz", ""),
                "score": float(diagnostic_score),
                "inliers": int(inliers),
                "inlier_ratio": float(inlier_ratio),
                "inlier_frames": int(inlier_frame_count),
                "strong_frames": int(strong_frame_count),
                "frame_inliers": {str(k): int(v) for k, v in frame_inliers.items()},
                "max_frame_inliers": int(max(frame_inliers.values()) if frame_inliers else 0),
                "ncc": float(ncc),
                "orientation": float(orientation),
                "ridge_score": float(ridge_score),
                "frames_detail": frame_records,
                "margin": 0.0,
            }
            candidate_records.append(candidate_metrics)
            if diagnostic_score > best_score:
                best_score = diagnostic_score
                best_metrics = candidate_metrics

        if candidate_records:
            candidate_records.sort(key=lambda item: item["score"], reverse=True)
            if target_filename:
                target_records = [
                    record for record in candidate_records
                    if record["filename"] == target_filename
                ]
                competitor_records = [
                    record for record in candidate_records
                    if record["filename"] != target_filename
                ]
                if target_records:
                    target_records.sort(key=lambda item: item["score"], reverse=True)
                    best_metrics = target_records[0]
                    competitor_score = competitor_records[0]["score"] if competitor_records else 0.0
                    second_target_score = target_records[1]["score"] if len(target_records) > 1 else 0.0
                    second_score = max(competitor_score, second_target_score)
                    best_metrics["margin"] = float(best_metrics["score"] - second_score)
                    if competitor_records:
                        best_metrics["competitor"] = {
                            "name": competitor_records[0]["name"],
                            "score": competitor_records[0]["score"],
                            "inliers": competitor_records[0]["inliers"],
                            "ridge_score": competitor_records[0]["ridge_score"],
                        }
                else:
                    best_metrics = {}
            else:
                best_metrics = candidate_records[0]
                second_score = candidate_records[1]["score"] if len(candidate_records) > 1 else 0.0
                best_metrics["margin"] = float(best_metrics["score"] - second_score)

            if best_metrics:
                print(f"[MATCHER] Best target={best_metrics.get('name')} score={best_metrics.get('score', 0.0):.2f} "
                      f"identity_margin={best_metrics.get('margin', 0.0):.2f} "
                      f"competitor={best_metrics.get('competitor', {}).get('name', 'none')}")

            if best_metrics and (
                    best_metrics["margin"] < thresholds["min_margin"] and
                    best_metrics.get("competitor")):
                print("[MATCHER] Rejecting target due to stronger enrolled-finger competitor.")

            if best_metrics and (not apply_thresholds or (
                    self.calibrated and
                    best_metrics["inliers"] >= thresholds["min_inliers"] and
                    best_metrics["inlier_ratio"] >= thresholds["min_inlier_ratio"] and
                    best_metrics["inlier_frames"] >= thresholds["min_inlier_frames"] and
                    best_metrics["max_frame_inliers"] >= thresholds["min_frame_inliers"] and
                    best_metrics["ncc"] >= thresholds["min_ncc"] and
                    best_metrics["orientation"] >= thresholds["min_orientation"] and
                    best_metrics["ridge_score"] >= thresholds["min_ridge_score"] and
                    best_metrics["margin"] >= thresholds["min_margin"])):
                best_result = (best_metrics["name"], best_metrics["inliers"])

        reject_reason = None
        if not best_result[0]:
            if not self.calibrated and apply_thresholds:
                reject_reason = "uncalibrated"
            elif not candidate_records:
                reject_reason = "no_valid_alignment"
            elif best_metrics.get("inliers", 0) < thresholds["min_inliers"]:
                reject_reason = "sift_weak"
            elif best_metrics.get("ncc", 0.0) < thresholds["min_ncc"]:
                reject_reason = "image_mismatch"
            elif best_metrics.get("orientation", 0.0) < thresholds["min_orientation"]:
                reject_reason = "ridge_orientation_mismatch"
            elif best_metrics.get("ridge_score", 0.0) < thresholds["min_ridge_score"]:
                reject_reason = "ridge_mismatch"
            elif best_metrics.get("competitor") and best_metrics.get("margin", 0.0) < thresholds["min_margin"]:
                reject_reason = "identity_conflict"
            else:
                reject_reason = "threshold_mismatch"

        self.last_verify_stats = {
            "frames": len(raw_frames),
            "keypoints": len(all_keypoints),
            "good_matches": len(good_matches),
            "candidates": len(candidate_votes),
            "best_inliers": best_inliers,
            "best": best_metrics,
            "calibrated": self.calibrated,
            "reject_reason": reject_reason,
            "thresholds": thresholds,
        }
        return best_result

    def get_enrolled_fingers(self, username):
        """Returns list of fingers for fprintd"""
        fingers = []
        prefix = f"{username}_"
        for filename in os.listdir(self.enroll_dir):
            if not filename.startswith(prefix) or not filename.endswith(".npz"):
                continue
            rest = filename[len(prefix):-4]
            if "_" in rest:
                continue
            if filename not in self.cached_templates:
                continue
            fingers.append(rest)
        return fingers

    def delete_user_fingers(self, username):
        """Wipes all fingers for a user"""
        prefix = f"{username}_"
        deleted = False
        for filename in sorted(os.listdir(self.enroll_dir)):
            if not filename.startswith(prefix):
                continue
            rest = filename[len(prefix):]
            stem = rest.rsplit(".", 1)[0] if "." in rest else rest
            if "_" in stem:
                continue
            try:
                os.remove(os.path.join(self.enroll_dir, filename))
                deleted = True
            except OSError:
                pass

        if deleted:
            self.rebuild_index()
