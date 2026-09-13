from egis_matcher.decision import MatchDecision, MatchOutcome
from egis_matcher.image_features import ImageFeatureExtractor
from egis_matcher.identity_matcher import IdentityMatcher
from egis_matcher.matcher_config import MatcherConfig


class MatcherCore:
    """Pure matcher over caller-supplied frames, indexes, templates, and policy."""

    def __init__(self, frame_spec=None, config=None, features=None):
        self.config = config or MatcherConfig()
        self.features = features or ImageFeatureExtractor(frame_spec=frame_spec)
        self.identity_matcher = IdentityMatcher(
            features=self.features,
            config=self.config,
        )

    def evaluate(self, raw_frames, **context):
        expected = self.features.frame_spec.byte_count
        if not raw_frames or any(len(frame) != expected for frame in raw_frames):
            metrics = {
                "frames": len(raw_frames),
                "expected_frame_bytes": expected,
                "reject_reason": "invalid_frame",
            }
            return MatchDecision(
                MatchOutcome.UNSCORABLE,
                reason="invalid_frame",
                metrics=metrics,
            )
        result, metrics = self.identity_matcher.verify_multiframe(
            raw_frames, **context)
        identity, score = result
        reason = metrics.get("reject_reason")
        if identity:
            outcome = MatchOutcome.ACCEPT
        elif reason == "uncalibrated":
            outcome = MatchOutcome.UNCALIBRATED
        elif reason in {"no_templates", "too_few_keypoints", "no_valid_alignment"}:
            outcome = MatchOutcome.UNSCORABLE
        else:
            outcome = MatchOutcome.REJECT
        return MatchDecision(
            outcome=outcome,
            identity=identity,
            score=int(score),
            reason=reason,
            metrics=metrics,
        )
