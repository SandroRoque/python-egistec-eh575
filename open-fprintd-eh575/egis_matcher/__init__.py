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
    "TouchIdentityMatcher": ("egis_matcher.atlas", "TouchIdentityMatcher"),
    "TouchDecision": ("egis_matcher.atlas", "TouchDecision"),
    "passes_thresholds": ("egis_matcher.policy", "passes_thresholds"),
    "SourceAfisEngine": ("egis_matcher.sourceafis", "SourceAfisEngine"),
    "SourceAfisTemplate": ("egis_matcher.sourceafis", "SourceAfisTemplate"),
    "FingerprintImage": ("egis_matcher.feature_engine", "FingerprintImage"),
    "FeatureRecord": ("egis_matcher.feature_engine", "FeatureRecord"),
    "PresentationGallery": ("egis_matcher.gallery", "PresentationGallery"),
    "StreamingGalleryMatcher": ("egis_matcher.gallery", "StreamingGalleryMatcher"),
    "CalibrationProfile": ("egis_matcher.calibration", "CalibrationProfile"),
    "FramePreprocessor": ("egis_matcher.calibration", "FramePreprocessor"),
    "UnsupportedCorrection": ("egis_matcher.calibration", "UnsupportedCorrection"),
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
    "TouchIdentityMatcher",
    "TouchDecision",
    "passes_thresholds",
    "SourceAfisEngine",
    "SourceAfisTemplate",
    "FingerprintImage",
    "FeatureRecord",
    "PresentationGallery",
    "StreamingGalleryMatcher",
    "CalibrationProfile",
    "FramePreprocessor",
    "UnsupportedCorrection",
)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module_name, attribute = _EXPORTS[name]
    from importlib import import_module
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
