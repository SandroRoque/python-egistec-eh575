from dataclasses import dataclass
import math
from typing import Mapping


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

MIN_HOMOGRAPHY_CORRESPONDENCES = 5


def validate_thresholds(values: Mapping):
    """Return normalized authentication thresholds or reject unsafe input."""
    missing = sorted(set(THRESHOLD_KEYS) - set(values))
    unknown = sorted(set(values) - set(THRESHOLD_KEYS))
    if missing:
        raise ValueError(f"missing threshold keys: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"unknown threshold keys: {', '.join(unknown)}")

    integer_bounds = {
        "min_inliers": (MIN_HOMOGRAPHY_CORRESPONDENCES, 10_000),
        "min_inlier_frames": (1, 64),
        "min_frame_inliers": (MIN_HOMOGRAPHY_CORRESPONDENCES, 10_000),
    }
    normalized = {}
    for key, (minimum, maximum) in integer_bounds.items():
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be an integer")
        if not math.isfinite(float(value)) or int(value) != value:
            raise ValueError(f"{key} must be a finite integer")
        value = int(value)
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        normalized[key] = value

    float_bounds = {
        "min_inlier_ratio": (0.0, 1.0),
        "min_margin": (0.0, 1_000_000.0),
        "min_ncc": (0.0, 1.0),
        "min_orientation": (0.0, 1.0),
        "min_ridge_score": (0.0, 1.0),
    }
    for key, (minimum, maximum) in float_bounds.items():
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be numeric")
        value = float(value)
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        normalized[key] = value

    if normalized["min_inliers"] < normalized["min_frame_inliers"]:
        raise ValueError("min_inliers must be at least min_frame_inliers")
    return {key: normalized[key] for key in THRESHOLD_KEYS}


def passes_thresholds(metrics: Mapping, thresholds: Mapping):
    if not metrics:
        return False
    return all((
        metrics.get("inliers", 0) >= thresholds["min_inliers"],
        metrics.get("inlier_ratio", 0.0) >= thresholds["min_inlier_ratio"],
        metrics.get("inlier_frames", 0) >= thresholds["min_inlier_frames"],
        metrics.get("max_frame_inliers", 0) >= thresholds["min_frame_inliers"],
        metrics.get("margin", 0.0) >= thresholds["min_margin"],
        metrics.get("ncc", 0.0) >= thresholds["min_ncc"],
        metrics.get("orientation", 0.0) >= thresholds["min_orientation"],
        metrics.get("ridge_score", 0.0) >= thresholds["min_ridge_score"],
    ))


@dataclass(frozen=True)
class ConfirmationPolicy:
    frames_per_attempt: int = 3
    required_consecutive_accepts: int = 2
    require_same_identity: bool = True

    def __post_init__(self):
        if self.frames_per_attempt < 1:
            raise ValueError("frames_per_attempt must be positive")
        if self.required_consecutive_accepts < 1:
            raise ValueError("required_consecutive_accepts must be positive")
        if not isinstance(self.require_same_identity, bool):
            raise ValueError("require_same_identity must be boolean")

    def to_dict(self):
        return {
            "frames_per_attempt": self.frames_per_attempt,
            "required_consecutive_accepts": self.required_consecutive_accepts,
            "require_same_identity": self.require_same_identity,
        }


class ConfirmationTracker:
    def __init__(self, policy=None):
        self.policy = policy or ConfirmationPolicy()
        self.consecutive_accepts = 0
        self.identity = None
        self.previous_identity = None
        self.last_reset_reason = None

    def record(self, accepted, identity=None):
        if not accepted:
            self.reset("not_accepted")
            return False
        if self.policy.require_same_identity:
            if self.identity is not None and identity != self.identity:
                previous = self.identity
                self.reset("identity_switch")
                self.previous_identity = previous
                self.identity = identity
                self.consecutive_accepts = 1
                self.last_reset_reason = "identity_switch"
                return False
            self.identity = identity
        self.last_reset_reason = None
        self.consecutive_accepts += 1
        return self.consecutive_accepts >= self.policy.required_consecutive_accepts

    def reset(self, reason="reset"):
        self.consecutive_accepts = 0
        self.identity = None
        self.last_reset_reason = reason
