import cv2
import logging
import numpy as np
import os
import time

from egis_matcher.core import MatcherCore
from egis_matcher.matcher_config import MatcherConfig
from egis_matcher.policy import THRESHOLD_KEYS
from egis_driver.persistence import Persistence
from egis_matcher.template_builder import TemplateBuilder
from egis_matcher.atlas import AtlasPolicy, FeatureAtlas, TouchIdentityMatcher
from egis_matcher.sequence import TouchTracker
from egis_driver.atlas_storage import load_atlas, save_atlas

logger = logging.getLogger("MATCHER")

MATCHER_VERSION = 5
TEMPLATE_SCHEMA_VERSION = 6
class FingerprintMatcher:
    def __init__(self, persistence=None, matcher_config=None, frame_spec=None):
        self.persistence = persistence or Persistence("/var/lib/open-fprintd")
        self.matcher_config = matcher_config or MatcherConfig()

        self.persistence.ensure_dirs()

        self.core = MatcherCore(frame_spec=frame_spec, config=self.matcher_config)
        self.features = self.core.features
        self.template_builder = TemplateBuilder(features=self.features)
        self.identity_matcher = self.core.identity_matcher

        self.flann = self._new_descriptor_matcher()

        self.descriptor_map = []
        self.descriptor_lookup = {}
        self.cached_templates = {}
        self.train_descriptors = None
        self._scoped_indexes = {}
        self.last_verify_stats = {}
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
        self.touch_atlases = {}
        self._load_thresholds()

        self.rebuild_index()

    def _load_thresholds(self):
        data = self.persistence.load_thresholds()
        if data is None:
            logger.warning("No calibration thresholds found; verification will fail closed.")
            return False
        try:
            if data.get("matcher_version") != MATCHER_VERSION:
                logger.warning("Calibration version mismatch; verification will fail closed.")
                return False
            if data.get("validated") is not True:
                logger.warning("Calibration thresholds are not validated; verification will fail closed.")
                return False
            thresholds = self._coerce_thresholds(data.get("thresholds", {}))
            self._set_default_thresholds(thresholds)
            target_thresholds = data.get("thresholds_by_target", {})
            self.thresholds_by_target = {
                target: self._coerce_thresholds(values)
                for target, values in target_thresholds.items()
            }
            self.calibrated = True
            self.threshold_source = self.persistence._threshold_file
            logger.info("Loaded calibrated thresholds")
            return True
        except Exception as e:
            logger.warning("Failed to load calibration thresholds: %s; verification will fail closed.", e)
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

    def analyze_touch(self, raw_frames):
        return self.template_builder.analyze_touch(raw_frames)

    def _new_descriptor_matcher(self):
        if self.matcher_config.random_seed is not None:
            cv2.setRNGSeed(self.matcher_config.random_seed)
        if self.matcher_config.index_backend == "bf":
            return cv2.BFMatcher(self.features.descriptor_norm, crossCheck=False)
        index_params = dict(algorithm=1, trees=5)
        search_params = dict(checks=50)
        return cv2.FlannBasedMatcher(index_params, search_params)

    def rebuild_index(self):
        logger.info("Rebuilding global FLANN Index...")
        start_t = time.time()

        all_descriptors = []
        self.descriptor_map = []
        self.descriptor_lookup = {}
        self.cached_templates = {}
        self.legacy_templates = []
        self._scoped_indexes = {}
        self.touch_atlases = {}

        current_idx_offset = 0

        for filename in self.persistence.list_template_files():
            if filename.endswith(".npy"):
                self.legacy_templates.append(filename)
                continue

            if not filename.endswith(".npz"):
                continue

            try:
                meta = self.persistence.load_template_metadata(filename)
            except Exception as e:
                logger.warning("Missing or corrupt metadata for %s: %s", filename, e)
                continue

            if meta.get("schema_version") != TEMPLATE_SCHEMA_VERSION:
                self.legacy_templates.append(filename)
                logger.warning("Ignoring incompatible template %s; re-enrollment required.", filename)
                continue

            try:
                npz = self.persistence.load_template_npz(filename)
            except Exception as e:
                logger.warning("Failed to load %s: %s", filename, e)
                continue

            try:
                num_templates = int(npz["num_templates"])
            except KeyError:
                logger.warning("Corrupt template %s: missing num_templates", filename)
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
                atlas_path = os.path.join(
                    self.persistence.atlas_dir, filename.removesuffix(".npz"))
                try:
                    atlas, atlas_manifest = load_atlas(
                        atlas_path, require_experimental=False)
                    if atlas_manifest.get("finger") != filename.removesuffix(".npz"):
                        raise ValueError("atlas identity does not match template")
                    self.touch_atlases[filename] = atlas
                except (OSError, KeyError, TypeError, ValueError) as error:
                    logger.warning("Touch atlas unavailable for %s: %s", filename, error)

        if all_descriptors:
            self.train_descriptors = np.vstack(all_descriptors)
            self.flann.clear()
            self.flann.add([self.train_descriptors])
            self.flann.train()
            logger.info("Index built in %.2fs. Total Features: %d",
                         time.time() - start_t, current_idx_offset)
        else:
            logger.info("Index is empty (no enrolled prints).")
            if self.legacy_templates:
                logger.info("%d legacy template file(s) require re-enrollment.",
                            len(self.legacy_templates))
            self.train_descriptors = None

    def _verification_index(self, username):
        if (
                self.matcher_config.index_scope != "username" or
                not username or
                self.train_descriptors is None):
            return (
                self.train_descriptors,
                self.descriptor_lookup,
                self.flann,
                "global",
            )

        cached = self._scoped_indexes.get(username)
        if cached is not None:
            return (*cached, "username")

        prefix = f"{username}_"
        selected = []
        for global_idx, owner in self.descriptor_lookup.items():
            filename = owner[0]
            if not filename.startswith(prefix):
                continue
            finger = filename[len(prefix):].rsplit(".", 1)[0]
            if "_" in finger:
                continue
            selected.append((global_idx, owner))

        if len(selected) < 2:
            scoped = (None, {}, None)
        else:
            descriptors = self.train_descriptors[
                np.asarray([global_idx for global_idx, _ in selected])
            ]
            lookup = {
                local_idx: owner
                for local_idx, (_, owner) in enumerate(selected)
            }
            flann = self._new_descriptor_matcher()
            flann.add([descriptors])
            flann.train()
            scoped = (descriptors, lookup, flann)

        self._scoped_indexes[username] = scoped
        return (*scoped, "username")

    def enroll_finger(self, name, raw_frames):
        logger.info("Enrolling %s (%d frames)...", name, len(raw_frames))

        save_data, meta, template_count = self.template_builder.build_payload(
            name,
            raw_frames,
            schema_version=TEMPLATE_SCHEMA_VERSION,
            matcher_version=MATCHER_VERSION,
        )
        if not save_data:
            return False

        safe_name = name.replace("/", "_")
        self.persistence.save_template(safe_name, save_data, meta)
        atlas = FeatureAtlas(
            self.features.frame_spec,
            AtlasPolicy(max_keyframes=256, max_keyframes_per_component=1),
        )
        for group in self.template_builder._normalize_touch_groups(raw_frames):
            if len(group) > 24:
                indices = np.linspace(0, len(group) - 1, 24, dtype=int)
                group = [group[index] for index in indices]
            tracker = TouchTracker(self.features.frame_spec, features=self.features)
            for sequence, raw in enumerate(group, 1):
                tracker.observe(raw, sequence)
            atlas.add_touch(tracker.observations)
        if not atlas.keyframes:
            logger.error("Enrollment produced no touch-atlas keyframes")
            self.persistence.delete_template_files([safe_name + ".npz"])
            return False
        self.persistence.replace_atlas(
            safe_name,
            atlas,
            lambda value, path: save_atlas(
                value, path, [], finger=safe_name,
                experimental=False, include_raw=False),
        )
        logger.info("Saved %d templates for %s", template_count, name)
        self.rebuild_index()
        return True

    def verify_finger(self, raw_frame):
        """Single-frame verification (legacy)."""
        return self.verify_finger_multiframe([raw_frame])

    def verify_finger_multiframe(
            self,
            raw_frames,
            username=None,
            finger_name=None,
            apply_thresholds=True,
            thresholds_override=None):
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
        return self.evaluate_multiframe(
            raw_frames,
            username=username,
            finger_name=finger_name,
            apply_thresholds=apply_thresholds,
            thresholds_override=thresholds_override,
        ).as_legacy_result()

    def evaluate_multiframe(
            self,
            raw_frames,
            username=None,
            finger_name=None,
            apply_thresholds=True,
            thresholds_override=None):
        """Return a structured decision for one completed frame window."""
        finger_name = self._normalize_verify_finger(finger_name)
        thresholds = thresholds_override or self._active_thresholds(username, finger_name)
        train_descriptors, descriptor_lookup, flann, index_scope = (
            self._verification_index(username)
        )
        decision = self.core.evaluate(
            raw_frames,
            username=username,
            finger_name=finger_name,
            train_descriptors=train_descriptors,
            descriptor_lookup=descriptor_lookup,
            cached_templates=self.cached_templates,
            flann=flann,
            thresholds=thresholds,
            calibrated=self.calibrated,
            legacy_templates=self.legacy_templates,
            apply_thresholds=apply_thresholds,
        )
        stats = dict(decision.metrics)
        stats["index_scope"] = index_scope
        self.last_verify_stats = stats
        return type(decision)(
            decision.outcome,
            decision.identity,
            decision.score,
            decision.reason,
            stats,
        )

    def get_enrolled_fingers(self, username):
        """Returns list of fingers for fprintd"""
        fingers = []
        prefix = f"{username}_"
        for filename in self.persistence.list_template_files():
            if not filename.startswith(prefix) or not filename.endswith(".npz"):
                continue
            rest = filename[len(prefix):-4]
            if "_" in rest:
                continue
            if filename not in self.cached_templates:
                continue
            fingers.append(rest)
        return fingers

    def new_touch_identity_matcher(self, username, finger_name=None):
        finger_name = self._normalize_verify_finger(finger_name)
        prefix = f"{username}_"
        atlases = {}
        for filename, atlas in self.touch_atlases.items():
            if not filename.startswith(prefix) or not filename.endswith(".npz"):
                continue
            identity = filename[len(prefix):-4]
            if "_" in identity or (finger_name and identity != finger_name):
                continue
            atlases[f"{username}_{identity}"] = atlas
        if not atlases:
            return None
        return TouchIdentityMatcher(atlases)

    def delete_user_fingers(self, username):
        """Wipes all fingers for a user"""
        prefix = f"{username}_"
        to_delete = []
        for filename in self.persistence.list_template_files():
            if not filename.startswith(prefix):
                continue
            rest = filename[len(prefix):]
            stem = rest.rsplit(".", 1)[0] if "." in rest else rest
            if "_" in stem:
                continue
            to_delete.append(filename)

        if to_delete:
            self.persistence.delete_template_files(to_delete)
            self.persistence.delete_atlases({
                filename.rsplit(".", 1)[0] for filename in to_delete
            })
            self.rebuild_index()
