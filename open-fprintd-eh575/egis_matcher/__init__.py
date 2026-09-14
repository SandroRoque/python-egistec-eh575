"""Hardware- and Linux-independent fingerprint matching primitives."""

_EXPORTS = {
    "ConfirmationPolicy": ("egis_matcher.policy", "ConfirmationPolicy"),
    "ConfirmationTracker": ("egis_matcher.policy", "ConfirmationTracker"),
    "FrameSpec": ("egis_matcher.frame", "FrameSpec"),
    "MatchDecision": ("egis_matcher.decision", "MatchDecision"),
    "MatchOutcome": ("egis_matcher.decision", "MatchOutcome"),
    "MatcherCore": ("egis_matcher.core", "MatcherCore"),
    "MatcherConfig": ("egis_matcher.matcher_config", "MatcherConfig"),
    "TouchTracker": ("egis_matcher.sequence", "TouchTracker"),
    "FeatureAtlas": ("egis_matcher.atlas", "FeatureAtlas"),
    "StreamingAtlasMatcher": ("egis_matcher.atlas", "StreamingAtlasMatcher"),
    "passes_thresholds": ("egis_matcher.policy", "passes_thresholds"),
    "FingerprintFeatureExtractor": ("egis_matcher.fingerprint_features", "FingerprintFeatureExtractor"),
    "FingerprintMatcher": ("egis_matcher.fingerprint_matcher", "FingerprintMatcher"),
    "FingerprintTemplate": ("egis_matcher.fingerprint_matcher", "FingerprintTemplate"),
    "Minutia": ("egis_matcher.fingerprint_features", "Minutia"),
}

__all__ = (
    "ConfirmationPolicy",
    "ConfirmationTracker",
    "FrameSpec",
    "MatchDecision",
    "MatchOutcome",
    "MatcherCore",
    "MatcherConfig",
    "TouchTracker",
    "FeatureAtlas",
    "StreamingAtlasMatcher",
    "passes_thresholds",
    "FingerprintFeatureExtractor",
    "FingerprintMatcher",
    "FingerprintTemplate",
    "Minutia",
)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module_name, attribute = _EXPORTS[name]
    from importlib import import_module
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
