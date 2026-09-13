from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class MatcherConfig:
    index_scope: str = "username"
    index_backend: str = "bf"
    random_seed: int | None = 0
    proposal_scope: str = "template"
    sift_ratio: float = 0.75
    max_candidates: int = 12
    ransac_reproj_threshold: float = 8.0
    min_scale: float = 0.45
    max_scale: float = 2.2
    max_angle: float = 55.0
    max_perspective_warp: float = 0.03

    @classmethod
    def from_dict(cls, values=None):
        values = values or {}
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"unknown matcher config keys: {', '.join(unknown)}")
        config = cls(**values)
        config.validate()
        return config

    def validate(self):
        if self.index_scope not in {"global", "username"}:
            raise ValueError("index_scope must be 'global' or 'username'")
        if self.index_backend not in {"bf", "flann"}:
            raise ValueError("index_backend must be 'bf' or 'flann'")
        if self.proposal_scope not in {"global", "template"}:
            raise ValueError("proposal_scope must be 'global' or 'template'")
        if self.random_seed is not None and self.random_seed < 0:
            raise ValueError("random_seed must be non-negative or null")
        if not 0.0 < self.sift_ratio < 1.0:
            raise ValueError("sift_ratio must be between 0 and 1")
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        if self.ransac_reproj_threshold <= 0:
            raise ValueError("ransac_reproj_threshold must be positive")
        if not 0.0 < self.min_scale < self.max_scale:
            raise ValueError("scale bounds are invalid")
        if self.max_angle <= 0 or self.max_perspective_warp <= 0:
            raise ValueError("geometry limits must be positive")

    def to_dict(self):
        return asdict(self)
