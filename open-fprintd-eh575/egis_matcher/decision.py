from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class MatchOutcome(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    UNSCORABLE = "unscorable"
    UNCALIBRATED = "uncalibrated"


@dataclass(frozen=True)
class MatchDecision:
    outcome: MatchOutcome
    identity: str | None = None
    score: int = 0
    reason: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def accepted(self):
        return self.outcome is MatchOutcome.ACCEPT

    def as_legacy_result(self):
        return (self.identity, self.score) if self.accepted else (None, 0)


@dataclass(frozen=True)
class MatchScore:
    """Offline evidence without an authentication outcome."""

    identity: str | None = None
    score: int = 0
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def as_legacy_result(self):
        return self.identity, self.score
