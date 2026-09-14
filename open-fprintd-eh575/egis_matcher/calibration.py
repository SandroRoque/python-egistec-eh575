"""Hardware-independent EH575 calibration representation and diagnostics."""

from dataclasses import dataclass
import base64
import hashlib
import json

import numpy as np

from egis_matcher.frame import FrameSpec


@dataclass(frozen=True)
class CalibrationProfile:
    """Versioned sensor evidence; opaque operations remain explicitly absent."""

    frame_spec: FrameSpec
    background: bytes
    bad_pixels: bytes
    sensor_gain: int | None = None
    sensor_vref_sel: int | None = None
    sensor_dc_p: int | None = None
    sensor_dc_c: int | None = None
    difference_lut: bytes | None = None
    provenance: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self):
        background = bytes(self.background)
        bad_pixels = bytes(self.bad_pixels)
        lut = None if self.difference_lut is None else bytes(self.difference_lut)
        if self.schema_version != 1 or self.frame_spec.dtype != "uint8":
            raise ValueError("unsupported calibration profile")
        if len(background) != self.frame_spec.byte_count:
            raise ValueError("background size does not match frame geometry")
        if len(bad_pixels) != self.frame_spec.byte_count or any(value not in (0, 1) for value in bad_pixels):
            raise ValueError("bad-pixel map must contain one binary byte per pixel")
        if lut is not None and len(lut) != 511:
            raise ValueError("difference LUT must cover signed byte differences")
        for value in (self.sensor_gain, self.sensor_vref_sel, self.sensor_dc_p, self.sensor_dc_c):
            if value is not None and not 0 <= value <= 255:
                raise ValueError("sensor calibration registers must be bytes")
        if not self.provenance or any(not value for value in self.provenance):
            raise ValueError("calibration provenance is required")
        object.__setattr__(self, "background", background)
        object.__setattr__(self, "bad_pixels", bad_pixels)
        object.__setattr__(self, "difference_lut", lut)
        object.__setattr__(self, "provenance", tuple(self.provenance))

    @property
    def correction_supported(self):
        return self.difference_lut is not None and not any(self.bad_pixels)

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "frame_spec": {
                "width": self.frame_spec.width,
                "height": self.frame_spec.height,
                "dtype": self.frame_spec.dtype,
            },
            "background_b64": base64.b64encode(self.background).decode("ascii"),
            "bad_pixels_b64": base64.b64encode(self.bad_pixels).decode("ascii"),
            "difference_lut_b64": (base64.b64encode(self.difference_lut).decode("ascii")
                                   if self.difference_lut is not None else None),
            "sensor_gain": self.sensor_gain,
            "sensor_vref_sel": self.sensor_vref_sel,
            "sensor_dc_p": self.sensor_dc_p,
            "sensor_dc_c": self.sensor_dc_c,
            "provenance": list(self.provenance),
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) != {
                "schema_version", "frame_spec", "background_b64", "bad_pixels_b64",
                "difference_lut_b64", "sensor_gain", "sensor_vref_sel", "sensor_dc_p",
                "sensor_dc_c", "provenance"}:
            raise ValueError("invalid calibration profile fields")
        try:
            decode = lambda value: base64.b64decode(value, validate=True)
            return cls(
                frame_spec=FrameSpec(**data["frame_spec"]),
                background=decode(data["background_b64"]),
                bad_pixels=decode(data["bad_pixels_b64"]),
                difference_lut=(decode(data["difference_lut_b64"])
                                if data["difference_lut_b64"] is not None else None),
                sensor_gain=data["sensor_gain"], sensor_vref_sel=data["sensor_vref_sel"],
                sensor_dc_p=data["sensor_dc_p"], sensor_dc_c=data["sensor_dc_c"],
                provenance=tuple(data["provenance"]), schema_version=data["schema_version"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("malformed calibration profile") from error

    @property
    def digest(self):
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class FrameDiagnostics:
    signed_background_difference: np.ndarray
    bad_pixel_count: int
    mean: float
    standard_deviation: float


class UnsupportedCorrection(RuntimeError):
    pass


class FramePreprocessor:
    """Apply only correction operations that have a complete recovered definition."""

    def __init__(self, profile):
        if not isinstance(profile, CalibrationProfile):
            raise TypeError("profile must be a CalibrationProfile")
        self.profile = profile

    def _raw(self, raw_frame):
        raw = bytes(raw_frame)
        if len(raw) != self.profile.frame_spec.byte_count:
            raise ValueError("raw frame size does not match calibration profile")
        return np.frombuffer(raw, dtype=np.uint8).reshape(
            self.profile.frame_spec.height, self.profile.frame_spec.width)

    def diagnose(self, raw_frame):
        raw = self._raw(raw_frame)
        background = np.frombuffer(self.profile.background, dtype=np.uint8).reshape(raw.shape)
        difference = background.astype(np.int16) - raw.astype(np.int16)
        difference.setflags(write=False)
        return FrameDiagnostics(
            signed_background_difference=difference,
            bad_pixel_count=int(np.count_nonzero(np.frombuffer(
                self.profile.bad_pixels, dtype=np.uint8))),
            mean=float(difference.mean()),
            standard_deviation=float(difference.std()),
        )

    def correct(self, raw_frame):
        diagnostics = self.diagnose(raw_frame)
        if self.profile.difference_lut is None:
            raise UnsupportedCorrection("Windows difference conversion is unresolved")
        if diagnostics.bad_pixel_count:
            raise UnsupportedCorrection("Windows bad-pixel replacement is unresolved")
        lut = np.frombuffer(self.profile.difference_lut, dtype=np.uint8)
        corrected = lut[diagnostics.signed_background_difference + 255]
        corrected = np.array(corrected, dtype=np.uint8, copy=True)
        corrected.setflags(write=False)
        return corrected
