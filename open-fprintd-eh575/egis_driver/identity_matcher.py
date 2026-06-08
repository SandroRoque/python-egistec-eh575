import logging

import cv2
import numpy as np

from egis_driver.image_features import ImageFeatureExtractor

logger = logging.getLogger("IDENTITY")


class IdentityMatcher:
    """Multi-frame identity verification against an enrolled template index."""

    def __init__(self, features=None):
        self.features = features or ImageFeatureExtractor()

    def verify_multiframe(
            self,
            raw_frames,
            *,
            username,
            finger_name,
            train_descriptors,
            descriptor_lookup,
            cached_templates,
            flann,
            thresholds,
            calibrated,
            legacy_templates,
            apply_thresholds=True):
        if train_descriptors is None:
            return (None, 0), {
                "frames": len(raw_frames),
                "keypoints": 0,
                "good_matches": 0,
                "candidates": 0,
                "best_inliers": 0,
                "calibrated": calibrated,
                "reject_reason": "no_templates",
                "legacy_templates": list(legacy_templates),
            }

        all_keypoints = []
        all_descriptors_list = []
        query_frame_ids = []
        query_images = []

        for frame_idx, raw_frame in enumerate(raw_frames):
            img_arr = self.features.raw_frame_to_image(raw_frame)
            img = self.features.preprocess(img_arr)
            query_images.append(img)
            kp, des = self.features.detect_features(img)

            if des is not None and len(kp) >= 4:
                all_keypoints.extend(kp)
                all_descriptors_list.append(des)
                query_frame_ids.extend([frame_idx] * len(des))

        if not all_descriptors_list or len(all_keypoints) < 4:
            logger.info("Too few keypoints across %d frames", len(raw_frames))
            return (None, 0), {
                "frames": len(raw_frames),
                "keypoints": len(all_keypoints),
                "good_matches": 0,
                "candidates": 0,
                "best_inliers": 0,
                "calibrated": calibrated,
                "reject_reason": "too_few_keypoints",
            }

        des_live = np.vstack(all_descriptors_list)

        logger.info("Combined %d keypoints from %d frames", len(all_keypoints), len(raw_frames))

        matches = flann.knnMatch(des_live, k=2)

        good_matches = []
        for pair in matches:
            if len(pair) < 2:
                continue
            m, n = pair
            if m.distance < 0.75 * n.distance:
                good_matches.append(m)

        logger.info("Good matches after ratio test: %d", len(good_matches))

        if len(good_matches) < 4:
            return (None, 0), {
                "frames": len(raw_frames),
                "keypoints": len(all_keypoints),
                "good_matches": len(good_matches),
                "candidates": 0,
                "best_inliers": 0,
                "calibrated": calibrated,
                "reject_reason": "sift_weak",
            }

        candidate_votes = {}
        user_prefix = f"{username}_" if username else None
        target_filename = f"{username}_{finger_name}.npz" if username and finger_name else None

        for m in good_matches:
            owner = descriptor_lookup.get(m.trainIdx)
            if owner is None:
                continue

            filename, t_idx, local_idx = owner
            if user_prefix and not filename.startswith(user_prefix):
                continue
            if user_prefix and "_" in filename[len(user_prefix):].rsplit(".", 1)[0]:
                continue

            key = (filename, t_idx)
            candidate_votes.setdefault(key, [])
            candidate_votes[key].append(cv2.DMatch(m.queryIdx, local_idx, m.imgIdx, m.distance))

        if not candidate_votes:
            return (None, 0), {
                "frames": len(raw_frames),
                "keypoints": len(all_keypoints),
                "good_matches": len(good_matches),
                "candidates": 0,
                "best_inliers": 0,
                "calibrated": calibrated,
                "reject_reason": "no_target_candidate",
            }

        logger.info("Candidates: %d", len(candidate_votes))

        top_candidates = sorted(candidate_votes.items(), key=lambda item: len(item[1]), reverse=True)[:5]
        best_result = (None, 0)
        best_score = 0.0
        best_inliers = 0
        best_metrics = {}
        candidate_records = []

        for i, (candidate_key, candidate_matches) in enumerate(top_candidates):
            if len(candidate_matches) < 4:
                continue

            filename, t_idx = candidate_key
            if filename not in cached_templates:
                continue
            template = cached_templates[filename][t_idx]
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

                homography, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 8.0)
                if homography is None or mask is None:
                    continue

                inliers = int(np.sum(mask))
                if inliers < 4:
                    continue

                sx = np.sqrt(homography[0, 0] ** 2 + homography[1, 0] ** 2)
                sy = np.sqrt(homography[0, 1] ** 2 + homography[1, 1] ** 2)
                angle = abs(np.degrees(np.arctan2(homography[1, 0], homography[0, 0])))
                perspective_warp = abs(homography[2, 0]) + abs(homography[2, 1])

                homography_valid = (
                    0.45 < sx < 2.2 and
                    0.45 < sy < 2.2 and
                    angle < 55 and
                    perspective_warp < 0.03
                )
                if not homography_valid:
                    continue

                ridge = self.features.ridge_consistency(query_images[frame_id], template, homography)
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
                logger.info("Candidate %d (%s): votes=%d, no valid frame alignments",
                            i + 1, filename, len(candidate_matches))
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

            logger.info(
                "diagnostic_score=%.2f (inliers=%d, ratio=%.2f, frames=%d, "
                "strong_frames=%d, frame_inliers=%s, dist_score=%.2f, "
                "ncc=%.2f, orientation=%.2f, ridge_score=%.2f)",
                diagnostic_score,
                inliers,
                inlier_ratio,
                inlier_frame_count,
                strong_frame_count,
                frame_inliers,
                dist_score,
                ncc,
                orientation,
                ridge_score,
            )

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
            best_metrics = self._select_best(candidate_records, target_filename)

            if best_metrics:
                logger.info(
                    "Best target=%s score=%.2f identity_margin=%.2f competitor=%s",
                    best_metrics.get("name"),
                    best_metrics.get("score", 0.0),
                    best_metrics.get("margin", 0.0),
                    best_metrics.get("competitor", {}).get("name", "none"),
                )

            if best_metrics and (
                    best_metrics["margin"] < thresholds["min_margin"] and
                    best_metrics.get("competitor")):
                logger.info("Rejecting target due to stronger enrolled-finger competitor.")

            if best_metrics and (not apply_thresholds or (
                    calibrated and
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
            if not calibrated and apply_thresholds:
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

        stats = {
            "frames": len(raw_frames),
            "keypoints": len(all_keypoints),
            "good_matches": len(good_matches),
            "candidates": len(candidate_votes),
            "best_inliers": best_inliers,
            "best": best_metrics,
            "calibrated": calibrated,
            "reject_reason": reject_reason,
            "thresholds": thresholds,
        }
        return best_result, stats

    def _select_best(self, candidate_records, target_filename):
        if not target_filename:
            best_metrics = candidate_records[0]
            second_score = candidate_records[1]["score"] if len(candidate_records) > 1 else 0.0
            best_metrics["margin"] = float(best_metrics["score"] - second_score)
            return best_metrics

        target_records = [
            record for record in candidate_records
            if record["filename"] == target_filename
        ]
        competitor_records = [
            record for record in candidate_records
            if record["filename"] != target_filename
        ]
        if not target_records:
            return {}

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
        return best_metrics
