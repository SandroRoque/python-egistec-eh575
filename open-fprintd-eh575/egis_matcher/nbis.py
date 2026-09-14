"""Fail-closed subprocess adapter for NIST MINDTCT and BOZORTH3."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import cv2
import numpy as np


@dataclass(frozen=True)
class NbisTemplate:
    xyt: bytes
    minutiae: int


@dataclass(frozen=True)
class ExtractionResult:
    template: NbisTemplate | None
    reason: str
    elapsed_ms: float


class NbisEngine:
    VERSION = "nbis-5.0.0-mindtct-bozorth3"

    def __init__(self, bin_dir=None, timeout=2.0, scale=1.0,
                 invert=False, enhance=False, min_minutiae=8):
        configured = bin_dir or os.environ.get("EGIS_NBIS_BIN")
        self.bin_dir = Path(configured) if configured else None
        self.timeout = float(timeout)
        self.scale = float(scale)
        self.invert = bool(invert)
        self.enhance = bool(enhance)
        self.min_minutiae = int(min_minutiae)
        if self.scale <= 0 or self.timeout <= 0 or self.min_minutiae < 1:
            raise ValueError("invalid NBIS engine configuration")
        self.cwsq = self._resolve("cwsq")
        self.mindtct = self._resolve("mindtct")
        self.bozorth3 = self._resolve("bozorth3")

    def _resolve(self, name):
        candidate = self.bin_dir / name if self.bin_dir else shutil.which(name)
        if candidate is None or not Path(candidate).is_file():
            raise FileNotFoundError(f"NBIS executable not found: {name}")
        return str(Path(candidate).resolve())

    def config(self):
        return {
            "engine": self.VERSION,
            "scale": self.scale,
            "invert": self.invert,
            "enhance": self.enhance,
            "min_minutiae": self.min_minutiae,
        }

    def extract(self, stitched):
        started = time.perf_counter()
        try:
            image = np.asarray(stitched.image, dtype=np.uint8)
            mask = np.asarray(stitched.mask, dtype=np.uint8) > 0
            if image.ndim != 2 or image.shape != mask.shape or not mask.any():
                raise ValueError("invalid stitched fingerprint")
            if self.scale != 1.0:
                size = (max(1, round(image.shape[1] * self.scale)),
                        max(1, round(image.shape[0] * self.scale)))
                image = cv2.resize(image, size, interpolation=cv2.INTER_CUBIC)
                mask = cv2.resize(mask.astype(np.uint8), size,
                                  interpolation=cv2.INTER_NEAREST).astype(bool)
            prepared = np.full(image.shape, 255, dtype=np.uint8)
            prepared[mask] = image[mask]
            if self.invert:
                prepared[mask] = 255 - prepared[mask]
            # Padding prevents the physical/canvas edge becoming minutiae.
            # NBIS' WSQ encoder rejects either dimension below 256 pixels.
            pad_y = max(24, (256 - prepared.shape[0] + 1) // 2)
            pad_x = max(24, (256 - prepared.shape[1] + 1) // 2)
            prepared = cv2.copyMakeBorder(
                prepared, pad_y, pad_y, pad_x, pad_x,
                cv2.BORDER_CONSTANT, value=255)
            with tempfile.TemporaryDirectory(prefix="egis-nbis-") as directory:
                root = Path(directory)
                root.chmod(0o700)
                raw = root / "probe.raw"
                raw.write_bytes(prepared.tobytes())
                raw.chmod(0o600)
                geometry = f"{prepared.shape[1]},{prepared.shape[0]},8,500"
                self._run([self.cwsq, "2.25", "wsq", str(raw),
                           "-raw_in", geometry], root)
                wsq = raw.with_suffix(".wsq")
                output = root / "features"
                command = [self.mindtct]
                if self.enhance:
                    command.append("-b")
                command.extend([str(wsq), str(output)])
                self._run(command, root)
                xyt_path = output.with_suffix(".xyt")
                if xyt_path.stat().st_size > 1024 * 1024:
                    raise ValueError("NBIS template exceeds size limit")
                xyt = xyt_path.read_bytes()
                minutiae = self._validate_xyt(xyt)
                if minutiae < self.min_minutiae:
                    return ExtractionResult(
                        None, "insufficient_minutiae",
                        (time.perf_counter() - started) * 1000.0)
                return ExtractionResult(
                    NbisTemplate(xyt, minutiae), "ok",
                    (time.perf_counter() - started) * 1000.0)
        except (OSError, ValueError, subprocess.SubprocessError):
            return ExtractionResult(
                None, "extraction_failed",
                (time.perf_counter() - started) * 1000.0)

    def compare(self, probe, enrolled):
        if not isinstance(probe, NbisTemplate) or not isinstance(enrolled, NbisTemplate):
            raise TypeError("NBIS comparison requires NbisTemplate values")
        with tempfile.TemporaryDirectory(prefix="egis-nbis-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            probe_path, enrolled_path = root / "probe.xyt", root / "enrolled.xyt"
            probe_path.write_bytes(probe.xyt)
            enrolled_path.write_bytes(enrolled.xyt)
            probe_path.chmod(0o600)
            enrolled_path.chmod(0o600)
            result = self._run(
                [self.bozorth3, str(probe_path), str(enrolled_path)], root)
            text = result.stdout.strip()
            if len(result.stdout) > 1024 or not text.isdigit():
                raise ValueError("invalid BOZORTH3 score")
            return int(text)

    def _run(self, command, cwd):
        return subprocess.run(
            command, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=self.timeout, check=True,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )

    @staticmethod
    def _validate_xyt(data):
        if len(data) > 1024 * 1024:
            raise ValueError("NBIS template exceeds size limit")
        count = 0
        for raw_line in data.splitlines():
            fields = raw_line.split()
            if len(fields) != 4:
                raise ValueError("malformed NBIS template")
            values = [int(value) for value in fields]
            if (any(value < 0 for value in values) or
                    values[0] > 100000 or values[1] > 100000 or
                    values[2] > 359 or values[3] > 100):
                raise ValueError("malformed NBIS template")
            count += 1
        return count
