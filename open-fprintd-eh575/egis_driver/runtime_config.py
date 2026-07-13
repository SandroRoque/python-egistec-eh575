import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    data_root: Path = Path("/var/lib/open-fprintd")
    install_root: Path = Path("/opt/egis-driver")

    @classmethod
    def from_environment(cls):
        return cls(
            data_root=Path(os.environ.get("EGIS_DATA_ROOT", "/var/lib/open-fprintd")),
            install_root=Path(os.environ.get("EGIS_INSTALL_ROOT", "/opt/egis-driver")),
        )

    @property
    def enrollment_dir(self):
        return self.data_root / "egis"

    @property
    def calibration_dir(self):
        return self.data_root / "egis-calibration"
