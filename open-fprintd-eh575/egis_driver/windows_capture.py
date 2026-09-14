"""Private USBPcap decoding and cross-platform raw-frame diagnostics."""

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np

from egis_driver.sequence_evaluation import read_touch
from egis_driver.streaming import FrameStatus


FRAME_BYTES = 103 * 52
MAX_CAPTURE_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class UsbPacket:
    number: int
    time_epoch: float
    endpoint: int
    transfer_type: str
    payload: bytes


def _hex_payload(value):
    compact = value.replace(":", "").strip()
    if not compact:
        return b""
    if len(compact) % 2 or any(character not in "0123456789abcdefABCDEF" for character in compact):
        raise ValueError("malformed USB payload")
    return bytes.fromhex(compact)


def decode_usbpcap(path, tshark="tshark"):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_CAPTURE_BYTES:
        raise ValueError("capture is missing or exceeds private analysis budget")
    executable = shutil.which(tshark) if not Path(tshark).is_absolute() else tshark
    if not executable:
        raise FileNotFoundError("tshark is required to decode USBPcap captures")
    command = [
        str(executable), "-r", str(path), "-Y", "usb", "-T", "fields",
        "-E", "separator=\\t", "-E", "occurrence=f",
        "-e", "frame.number", "-e", "frame.time_epoch",
        "-e", "usb.endpoint_address", "-e", "usb.transfer_type",
        "-e", "usb.capdata",
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace")
    packets = []
    assert process.stdout is not None
    for line in process.stdout:
        fields = line.rstrip("\r\n").split("\t")
        if len(fields) != 5 or not fields[0] or not fields[1]:
            continue
        endpoint_text = fields[2].split(",", 1)[0]
        try:
            endpoint = int(endpoint_text, 0) if endpoint_text else 0
            packets.append(UsbPacket(
                int(fields[0]), float(fields[1]), endpoint,
                fields[3].split(",", 1)[0], _hex_payload(fields[4].split(",", 1)[0])))
        except ValueError as error:
            process.kill()
            raise ValueError(f"invalid tshark record at frame {fields[0]}") from error
    stderr = process.stderr.read(8192) if process.stderr else ""
    if process.wait() != 0:
        raise RuntimeError(f"tshark failed: {stderr.strip()}")
    return packets


def analyze_packets(packets):
    endpoint_counts = Counter(f"0x{packet.endpoint:02x}" for packet in packets)
    commands = Counter()
    response_lengths = Counter()
    frames = []
    traffic_runs = []
    origin = packets[0].time_epoch if packets else 0.0

    def record_run(packet, kind):
        offset_ms = round((packet.time_epoch - origin) * 1000, 3)
        if traffic_runs and traffic_runs[-1]["kind"] == kind:
            traffic_runs[-1]["count"] += 1
            traffic_runs[-1]["last_offset_ms"] = offset_ms
            return
        traffic_runs.append({
            "kind": kind,
            "count": 1,
            "first_offset_ms": offset_ms,
            "last_offset_ms": offset_ms,
        })

    for packet in packets:
        if packet.endpoint == 0x01 and packet.payload:
            signature = packet.payload[:32].hex()
            commands[signature] += 1
            record_run(packet, f"out:{signature}")
        elif packet.endpoint == 0x82:
            response_lengths[len(packet.payload)] += 1
            # This boundary is a candidate until fresh captures confirm whether
            # USBPcap presents a header-free 5,356-byte image transfer.
            if len(packet.payload) >= FRAME_BYTES:
                frames.append(packet.payload[:FRAME_BYTES])
                record_run(packet, f"in:image-candidate:{len(packet.payload)}")
            else:
                # Length and a short prefix are sufficient to correlate status
                # packets while keeping image bytes out of the JSON report.
                prefix = packet.payload[:16].hex()
                record_run(packet, f"in:response:{len(packet.payload)}:{prefix}")
        else:
            record_run(packet, f"endpoint:0x{packet.endpoint:02x}:{len(packet.payload)}")
    return {
        "packet_count": len(packets),
        "endpoint_counts": dict(sorted(endpoint_counts.items())),
        "out_command_signatures": dict(sorted(commands.items())),
        "in_payload_lengths": {str(key): value for key, value in sorted(response_lengths.items())},
        "candidate_frame_count": len(frames),
        "candidate_boundary_confirmed": False,
        "traffic_runs": traffic_runs,
    }, frames


def write_capture_analysis(captures, output, tshark="tshark"):
    output = Path(output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    output.chmod(0o700)
    phases = []
    for capture in captures:
        packets = decode_usbpcap(capture, tshark=tshark)
        report, frames = analyze_packets(packets)
        frame_file = output / f"{Path(capture).stem}.frames.bin"
        frame_file.write_bytes(b"".join(frames))
        frame_file.chmod(0o600)
        phases.append({
            "phase": Path(capture).stem,
            "capture_sha256": hashlib.sha256(Path(capture).read_bytes()).hexdigest(),
            "frames_file": frame_file.name,
            **report,
        })
    result = {"schema_version": 1, "private": True, "frame_geometry": [103, 52], "phases": phases}
    manifest = output / "analysis.json"
    manifest.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest.chmod(0o600)
    return result


def _frame_statistics(frames):
    if not frames:
        return None
    stack = np.stack([
        np.frombuffer(frame, dtype=np.uint8).reshape(52, 103).astype(np.float32)
        for frame in frames
    ])
    consecutive = np.abs(np.diff(stack, axis=0)) if len(stack) > 1 else np.empty((0, 52, 103))
    return {
        "frame_count": len(stack),
        "mean_intensity": float(stack.mean()),
        "mean_frame_contrast": float(np.mean(np.std(stack, axis=(1, 2)))),
        "fixed_pattern_std": float(np.std(np.mean(stack, axis=0))),
        "temporal_noise": float(np.mean(np.std(stack, axis=0))),
        "mean_consecutive_absolute_difference": (
            float(consecutive.mean()) if consecutive.size else None),
    }


def compare_raw_captures(windows_analysis, linux_sequences):
    root = Path(windows_analysis)
    manifest = json.loads((root / "analysis.json").read_text(encoding="utf-8"))
    windows = {}
    for phase in manifest["phases"]:
        payload = (root / phase["frames_file"]).read_bytes()
        if len(payload) % FRAME_BYTES:
            raise ValueError("Windows frame payload is truncated")
        windows[phase["phase"]] = _frame_statistics(
            [payload[index:index + FRAME_BYTES] for index in range(0, len(payload), FRAME_BYTES)])
    linux_frames = []
    for directory in linux_sequences:
        _, messages, spec = read_touch(directory)
        if (spec.width, spec.height) != (103, 52):
            raise ValueError("Linux sequence geometry differs from EH575")
        linux_frames.extend(message.pixels for message in messages
                            if message.status is FrameStatus.VALID)
    return {
        "schema_version": 1, "private": True,
        "comparison_kind": "raw_transport_frames",
        "windows_phases": windows,
        "linux": _frame_statistics(linux_frames),
        "limitations": [
            "USBPcap observes sensor transport bytes, not host-corrected Windows images",
            "capture conditions are paired but not the same physical touch",
        ],
    }
