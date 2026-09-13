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
    "passes_thresholds": ("egis_matcher.policy", "passes_thresholds"),
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
    "passes_thresholds",
)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module_name, attribute = _EXPORTS[name]
    from importlib import import_module
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
