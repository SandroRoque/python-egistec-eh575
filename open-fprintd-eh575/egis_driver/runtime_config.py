import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    data_root: Path = Path("/var/lib/open-fprintd")
    install_root: Path = Path("/opt/egis-driver")
    match_mode: str = "window"

    def __post_init__(self):
        if self.match_mode != "window":
            raise ValueError("EGIS_MATCH_MODE must be window; shadow matching has moved to offline egis-lab tooling")

    @classmethod
    def from_environment(cls):
        return cls(
            data_root=Path(os.environ.get("EGIS_DATA_ROOT", "/var/lib/open-fprintd")),
            install_root=Path(os.environ.get("EGIS_INSTALL_ROOT", "/opt/egis-driver")),
            match_mode=os.environ.get("EGIS_MATCH_MODE", "window"),
        )

    @property
    def enrollment_dir(self):
        return self.data_root / "egis"

    @property
    def calibration_dir(self):
        return self.data_root / "egis-calibration"

    @property
    def gallery_dir(self):
        return self.data_root / "egis-gallery"

    @property
    def atlas_dir(self):
        return self.data_root / "egis-atlas"
