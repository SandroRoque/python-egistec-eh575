from dataclasses import dataclass
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
