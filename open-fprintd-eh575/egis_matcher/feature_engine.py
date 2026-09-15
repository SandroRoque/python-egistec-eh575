"""Extractor-neutral fingerprint image and opaque feature-record contracts."""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class FingerprintImage:
    image: np.ndarray
    mask: np.ndarray

    def __post_init__(self):
        image = np.array(self.image, dtype=np.uint8, copy=True)
        mask = np.array(self.mask, dtype=np.uint8, copy=True)
        if image.ndim != 2 or mask.shape != image.shape or not image.size:
            raise ValueError("fingerprint image and mask geometry differ")
        image.setflags(write=False)
        mask.setflags(write=False)
        object.__setattr__(self, "image", image)
        object.__setattr__(self, "mask", mask)

    @classmethod
    def from_frame(cls, image, border=4):
        image = np.asarray(image, dtype=np.uint8)
        if border < 0 or border * 2 >= min(image.shape):
            raise ValueError("invalid fingerprint image border")
        mask = np.zeros(image.shape, dtype=np.uint8)
        mask[border:image.shape[0] - border,
             border:image.shape[1] - border] = 255
        rendered = np.full(image.shape, 255, dtype=np.uint8)
        rendered[mask != 0] = image[mask != 0]
        return cls(rendered, mask)


@dataclass(frozen=True)
class FeatureRecord:
    engine: str
    version: str
    data: bytes

    def __post_init__(self):
        data = bytes(self.data)
        if not self.engine or not self.version or not data or len(data) > 8 * 1024 * 1024:
            raise ValueError("invalid feature record")
        object.__setattr__(self, "data", data)
