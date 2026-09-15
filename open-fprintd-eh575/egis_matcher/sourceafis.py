"""SourceAFIS adapter for stitched-touch experiments.

The Java process owns all fingerprint feature extraction and identity scoring.
Python only transports grayscale images and opaque templates across a deliberately
small protocol boundary.
"""

from dataclasses import dataclass
import base64
import os
from pathlib import Path
import select
import subprocess
import threading
import time

import cv2
import numpy as np

from egis_matcher.stitching import StitchedPrint
from egis_matcher.feature_engine import FeatureRecord, FingerprintImage


@dataclass(frozen=True)
class SourceAfisTemplate:
    data: bytes

    def __post_init__(self):
        data = bytes(self.data)
        if not data or len(data) > 8 * 1024 * 1024:
            raise ValueError("invalid SourceAFIS template size")
        object.__setattr__(self, "data", data)


@dataclass(frozen=True)
class SourceAfisExtraction:
    template: SourceAfisTemplate | None
    reason: str
    elapsed_ms: float


class SourceAfisEngine:
    VERSION = "3.18.1"

    def __init__(self, home=None, scale=1.0, invert=False, dpi=500, timeout=10.0):
        configured = home or os.environ.get("EGIS_SOURCEAFIS_HOME")
        if not configured:
            configured = "/opt/sourceafis-3.18.1"
        self.home = Path(configured)
        self.scale = float(scale)
        self.invert = bool(invert)
        self.dpi = int(dpi)
        self.timeout = float(timeout)
        if (not 0.5 <= self.scale <= 4.0 or not 200 <= self.dpi <= 2000 or
                not 0 < self.timeout <= 60):
            raise ValueError("invalid SourceAFIS engine configuration")
        self._process = None
        self._lock = threading.Lock()

    def config(self):
        return {
            "engine": "sourceafis",
            "version": self.VERSION,
            "scale": self.scale,
            "invert": self.invert,
            "dpi": self.dpi,
        }

    @property
    def available(self):
        return (self.home / "bin" / "egis-sourceafis-worker").is_file()

    def _start(self):
        if self._process is not None and self._process.poll() is None:
            return
        launcher = self.home / "bin" / "egis-sourceafis-worker"
        if not launcher.is_file():
            raise FileNotFoundError(f"SourceAFIS worker not found: {launcher}")
        self._process = subprocess.Popen(
            [str(launcher)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="ascii", bufsize=1,
        )
        response = self._exchange("PING")
        if response != f"PONG\t{self.VERSION}":
            self.close()
            raise RuntimeError(f"unexpected SourceAFIS worker response: {response}")

    def _exchange(self, command):
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("SourceAFIS worker is not running")
        process.stdin.write(command + "\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], self.timeout)
        if not ready:
            process.kill()
            process.wait(timeout=2)
            self._process = None
            raise RuntimeError("SourceAFIS worker timed out")
        response = process.stdout.readline().rstrip("\n")
        if not response:
            detail = process.stderr.read(4096) if process.stderr else ""
            raise RuntimeError(f"SourceAFIS worker stopped: {detail.strip()}")
        if response.startswith("ERROR\t"):
            raise RuntimeError(response.split("\t", 1)[1])
        return response

    def extract(self, stitched):
        started = time.perf_counter()
        if not isinstance(stitched, (StitchedPrint, FingerprintImage)):
            raise TypeError("SourceAFIS extraction requires a fingerprint image")
        image = stitched.image
        if self.scale != 1.0:
            image = cv2.resize(
                image, None, fx=self.scale, fy=self.scale,
                interpolation=cv2.INTER_CUBIC)
        if self.invert:
            image = 255 - image
        if image.size > 8 * 1024 * 1024:
            return SourceAfisExtraction(None, "image_too_large", 0.0)
        try:
            with self._lock:
                self._start()
                encoded = base64.b64encode(image.tobytes()).decode("ascii")
                response = self._exchange(
                    f"EXTRACT\t{image.shape[1]}\t{image.shape[0]}\t{self.dpi}\t{encoded}")
            kind, payload = response.split("\t", 1)
            if kind != "TEMPLATE":
                raise RuntimeError(f"unexpected extraction response: {kind}")
            data = base64.b64decode(payload, validate=True)
            return SourceAfisExtraction(
                SourceAfisTemplate(data), "ok",
                (time.perf_counter() - started) * 1000.0)
        except (RuntimeError, ValueError) as error:
            return SourceAfisExtraction(
                None, f"extraction_failed:{type(error).__name__}",
                (time.perf_counter() - started) * 1000.0)

    def compare(self, probe, enrolled):
        if not isinstance(probe, SourceAfisTemplate) or not isinstance(enrolled, SourceAfisTemplate):
            raise TypeError("SourceAFIS comparison requires SourceAfisTemplate values")
        with self._lock:
            self._start()
            left = base64.b64encode(probe.data).decode("ascii")
            right = base64.b64encode(enrolled.data).decode("ascii")
            response = self._exchange(f"COMPARE\t{left}\t{right}")
        kind, value = response.split("\t", 1)
        if kind != "SCORE":
            raise RuntimeError(f"unexpected comparison response: {kind}")
        return float(value)

    def extract_record(self, fingerprint):
        result = self.extract(fingerprint)
        record = (FeatureRecord("sourceafis", self.VERSION, result.template.data)
                  if result.template is not None else None)
        return record, result.reason, result.elapsed_ms

    def compare_records(self, probe, enrolled):
        for record in (probe, enrolled):
            if (not isinstance(record, FeatureRecord) or
                    record.engine != "sourceafis" or record.version != self.VERSION):
                raise ValueError("incompatible SourceAFIS feature record")
        return self.compare(
            SourceAfisTemplate(probe.data), SourceAfisTemplate(enrolled.data))

    def close(self):
        process, self._process = self._process, None
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        self.close()
