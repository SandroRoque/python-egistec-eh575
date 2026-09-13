from dataclasses import dataclass


@dataclass(frozen=True)
class FrameSpec:
    width: int
    height: int
    dtype: str = "uint8"

    @property
    def byte_count(self):
        return self.width * self.height
