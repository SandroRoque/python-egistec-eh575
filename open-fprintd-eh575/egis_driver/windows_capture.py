"""Private USBPcap decoding and cross-platform raw-frame diagnostics."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess

import numpy as np

from egis_driver.sequence_evaluation import read_touch
from egis_driver.streaming import FrameStatus


FRAME_BYTES = 103 * 52
WINDOWS_FRAME_PREFIX_BYTES = 5120
WINDOWS_FRAME_SUFFIX_BYTES = FRAME_BYTES - WINDOWS_FRAME_PREFIX_BYTES
WINDOWS_FRAME_FRAGMENT_TIMEOUT_SECONDS = 0.100
MAX_CAPTURE_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class UsbPacket:
    number: int
    time_epoch: float
    endpoint: int
    transfer_type: str
    payload: bytes
    irp_id: int = 0
    status: int = 0
    function: int = 0
    info: int = 0
    bus: int = 0
    device: int = 0
    declared_length: int = 0

    @property
    def is_completion(self):
        return bool(self.info & 1)

    @property
    def direction(self):
        return "in" if self.endpoint & 0x80 else "out"


def _hex_payload(value):
    compact = value.replace(":", "").strip()
    if not compact:
        return b""
    if len(compact) % 2 or any(character not in "0123456789abcdefABCDEF" for character in compact):
        raise ValueError("malformed USB payload")
    return bytes.fromhex(compact)


def _decode_usbpcap_pcap(path):
    """Decode USBPcap's DLT_USBPCAP records without Wireshark.

    USBPcap stores a variable-length pseudo-header in each libpcap record. The
    endpoint is at byte 21, transfer type at byte 22, and the captured payload
    begins at the two-byte header length advertised at byte 0. We intentionally
    retain only payload bytes and sanitized packet metadata.
    """
    raw = Path(path).read_bytes()
    if len(raw) < 24 or raw[:4] not in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        raise ValueError("not a little-endian libpcap capture")
    magic = raw[:4]
    endian = "<"
    timestamp_divisor = 1_000_000_000.0 if magic == b"\x4d\x3c\xb2\xa1" else 1_000_000.0
    _, _, _, _, _, _, link_type = struct.unpack_from(endian + "IHHIIII", raw, 0)
    if link_type != 249:  # DLT_USBPCAP / LINKTYPE_USBPCAP
        raise ValueError(f"unsupported libpcap link type {link_type}")
    offset = 24
    packets = []
    number = 0
    while offset + 16 <= len(raw):
        seconds, micros, captured, _original = struct.unpack_from(
            endian + "IIII", raw, offset)
        offset += 16
        end = offset + captured
        if end > len(raw):
            raise ValueError("truncated libpcap record")
        record = raw[offset:end]
        offset = end
        if len(record) < 27:
            continue
        header_length = struct.unpack_from(endian + "H", record, 0)[0]
        if header_length < 27 or header_length > len(record):
            raise ValueError("invalid USBPcap header length")
        irp_id = struct.unpack_from(endian + "Q", record, 2)[0]
        status = struct.unpack_from(endian + "I", record, 10)[0]
        function = struct.unpack_from(endian + "H", record, 14)[0]
        info = record[16]
        bus = struct.unpack_from(endian + "H", record, 17)[0]
        device = struct.unpack_from(endian + "H", record, 19)[0]
        endpoint = record[21]
        transfer_type = str(record[22])
        declared_length = struct.unpack_from(endian + "I", record, 23)[0]
        payload = record[header_length:]
        if len(payload) > declared_length:
            payload = payload[:declared_length]
        number += 1
        packets.append(UsbPacket(
            number, seconds + micros / timestamp_divisor, endpoint,
            transfer_type, payload, irp_id, status, function, info,
            bus, device, declared_length))
    return packets


def decode_usbpcap(path, tshark="tshark"):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_CAPTURE_BYTES:
        raise ValueError("capture is missing or exceeds private analysis budget")
    # Classic libpcap/DLT_USBPCAP has a small stable header and can be decoded
    # without a Wireshark installation. Tshark remains the pcapng fallback.
    with path.open("rb") as capture:
        if capture.read(4) in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
            return _decode_usbpcap_pcap(path)
    executable = shutil.which(tshark) if not Path(tshark).is_absolute() else tshark
    if not executable:
        return _decode_usbpcap_pcap(path)
    command = [
        str(executable), "-r", str(path), "-Y", "usb", "-T", "fields",
        "-E", "separator=\\t", "-E", "occurrence=f",
        "-e", "frame.number", "-e", "frame.time_epoch",
        "-e", "usb.endpoint_address", "-e", "usb.transfer_type",
        "-e", "usb.capdata", "-e", "usb.irp_id", "-e", "usb.usbd_status",
        "-e", "usb.urb_function", "-e", "usb.irp_info", "-e", "usb.bus_id",
        "-e", "usb.device_address", "-e", "usb.data_len",
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace")
    packets = []
    assert process.stdout is not None
    for line in process.stdout:
        fields = line.rstrip("\r\n").split("\t")
        if len(fields) != 12 or not fields[0] or not fields[1]:
            continue
        endpoint_text = fields[2].split(",", 1)[0]
        try:
            endpoint = int(endpoint_text, 0) if endpoint_text else 0
            numeric = lambda value: int(value.split(",", 1)[0], 0) if value else 0
            packets.append(UsbPacket(
                int(fields[0]), float(fields[1]), endpoint,
                fields[3].split(",", 1)[0], _hex_payload(fields[4].split(",", 1)[0]),
                numeric(fields[5]), numeric(fields[6]), numeric(fields[7]),
                numeric(fields[8]), numeric(fields[9]), numeric(fields[10]),
                numeric(fields[11])))
        except ValueError as error:
            process.kill()
            raise ValueError(f"invalid tshark record at frame {fields[0]}") from error
    stderr = process.stderr.read(8192) if process.stderr else ""
    if process.wait() != 0:
        raise RuntimeError(f"tshark failed: {stderr.strip()}")
    return packets


def _device_descriptors(packets):
    descriptors = {}
    for packet in packets:
        payload = packet.payload
        if len(payload) >= 12 and payload[0:2] == b"\x12\x01":
            vendor, product = struct.unpack_from("<HH", payload, 8)
            descriptors[(packet.bus, packet.device)] = (vendor, product)
    return descriptors


def _payload_summary(payload):
    values = np.frombuffer(payload, dtype=np.uint8)
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_count": len(payload),
        "mean": float(values.mean()),
        "standard_deviation": float(values.std()),
        "minimum": int(values.min()),
        "maximum": int(values.max()),
    }


def _pair_urbs(packets):
    pending = Counter()
    paired = 0
    orphan_completions = 0
    failed_completions = 0
    for packet in packets:
        if not packet.irp_id:
            continue
        if packet.is_completion:
            if packet.status:
                failed_completions += 1
            if pending[packet.irp_id]:
                pending[packet.irp_id] -= 1
                paired += 1
            else:
                orphan_completions += 1
        else:
            pending[packet.irp_id] += 1
    return {
        "paired": paired,
        "orphan_requests": sum(pending.values()),
        "orphan_completions": orphan_completions,
        "failed_completions": failed_completions,
    }


def analyze_packets(packets, vendor_id=0x1C7A, product_id=0x0575):
    descriptors = _device_descriptors(packets)
    descriptor_addresses = {
        address for address, identity in descriptors.items()
        if identity == (vendor_id, product_id)
    }
    magic_addresses = {
        (packet.bus, packet.device) for packet in packets
        if packet.payload.startswith((b"EGIS", b"SIGE"))
    }
    sensor_addresses = descriptor_addresses | magic_addresses
    sensor_packets = [
        packet for packet in packets
        if (packet.bus, packet.device) in sensor_addresses
    ]
    endpoint_counts = Counter(f"0x{packet.endpoint:02x}" for packet in sensor_packets)
    commands = Counter()
    response_lengths = Counter()
    frames = []
    calibration = []
    unlinked_large = []
    protocol_events = []
    traffic_runs = []
    origin = sensor_packets[0].time_epoch if sensor_packets else 0.0
    pending = {}
    frame_prefixes = {}
    orphan_frame_prefixes = 0
    orphan_frame_suffixes = 0

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

    for packet in sensor_packets:
        address = (packet.bus, packet.device)
        payload = packet.payload
        expired = [key for key, (prefix_packet, _) in frame_prefixes.items()
                   if packet.time_epoch - prefix_packet.time_epoch
                   > WINDOWS_FRAME_FRAGMENT_TIMEOUT_SECONDS]
        orphan_frame_prefixes += len(expired)
        for key in expired:
            frame_prefixes.pop(key)
        if not payload:
            if packet.status:
                record_run(packet, f"usb-error:0x{packet.status:08x}")
            continue
        if packet.endpoint == 0x82 and len(payload) == WINDOWS_FRAME_PREFIX_BYTES:
            if address in frame_prefixes:
                orphan_frame_prefixes += 1
            frame_prefixes[address] = (packet, payload)
            record_run(packet, f"raw-frame-prefix:in:{len(payload)}")
            continue
        if packet.endpoint == 0x82 and len(payload) == WINDOWS_FRAME_SUFFIX_BYTES:
            prefix = frame_prefixes.pop(address, None)
            if (prefix is not None and
                    packet.time_epoch - prefix[0].time_epoch
                    <= WINDOWS_FRAME_FRAGMENT_TIMEOUT_SECONDS):
                frame = prefix[1] + payload
                frames.append(frame)
                protocol_events.append({
                    "kind": "raw-frame",
                    "offset_ms": round((prefix[0].time_epoch - origin) * 1000, 3),
                    "bus": packet.bus, "device": packet.device,
                    "endpoint": "0x82", "direction": "in",
                    "length": len(frame), "fragment_lengths": [
                        WINDOWS_FRAME_PREFIX_BYTES, WINDOWS_FRAME_SUFFIX_BYTES],
                    "fragment_gap_ms": round(
                        (packet.time_epoch - prefix[0].time_epoch) * 1000, 3),
                    "sha256": hashlib.sha256(frame).hexdigest(),
                    "linked_opcode": None,
                })
                record_run(packet, f"raw-frame-fragmented:in:{len(frame)}")
            else:
                orphan_frame_suffixes += 1
                record_run(packet, f"orphan-frame-suffix:in:{len(payload)}")
            continue
        if packet.endpoint == 0x82 and address in frame_prefixes:
            frame_prefixes.pop(address)
            orphan_frame_prefixes += 1
        if payload.startswith(b"EGIS") and len(payload) >= 5:
            signature = packet.payload[:32].hex()
            commands[signature] += 1
            expected = (int.from_bytes(payload[5:7], "big")
                        if len(payload) >= 7 else None)
            command = {
                "offset_ms": round((packet.time_epoch - origin) * 1000, 3),
                "bus": packet.bus, "device": packet.device,
                "endpoint": f"0x{packet.endpoint:02x}",
                "direction": packet.direction, "opcode": f"0x{payload[4]:02x}",
                "arguments_hex": payload[5:32].hex(), "expected_length": expected,
            }
            protocol_events.append({"kind": "command", **command})
            pending[address] = command
            record_run(packet, f"command:{command['opcode']}:{command['direction']}")
        elif payload.startswith(b"SIGE"):
            response_lengths[len(packet.payload)] += 1
            protocol_events.append({
                "kind": "response",
                "offset_ms": round((packet.time_epoch - origin) * 1000, 3),
                "bus": packet.bus, "device": packet.device,
                "endpoint": f"0x{packet.endpoint:02x}",
                "direction": packet.direction, "length": len(payload),
                "status_hex": payload[4:7].hex(),
            })
            pending.pop(address, None)
            record_run(packet, f"response:{len(payload)}:{packet.direction}")
        elif len(payload) >= FRAME_BYTES:
            command = pending.get(address)
            linked_length = command and command["expected_length"] == len(payload)
            if linked_length and command["opcode"] == "0x73" and packet.direction == "out":
                kind = "calibration-upload"
                calibration.append(payload)
            elif (linked_length and command["opcode"] in ("0x64", "0x73")
                  and packet.direction == "in"):
                kind = "raw-frame"
                frames.append(payload[:FRAME_BYTES])
            else:
                kind = "unlinked-large-transfer"
                unlinked_large.append(payload)
            protocol_events.append({
                "kind": kind,
                "offset_ms": round((packet.time_epoch - origin) * 1000, 3),
                "bus": packet.bus, "device": packet.device,
                "endpoint": f"0x{packet.endpoint:02x}",
                "direction": packet.direction, "length": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "linked_opcode": command["opcode"] if command else None,
            })
            record_run(packet, f"{kind}:{packet.direction}:{len(payload)}")
        else:
            record_run(packet, f"data:{packet.direction}:{len(payload)}")
    warnings = []
    if descriptor_addresses and not commands:
        warnings.append("EH575 descriptor found but no EGIS protocol traffic was captured")
    if commands and not (frames or calibration):
        warnings.append("EGIS traffic found but no linked full-size transfer was captured")
    orphan_frame_prefixes += len(frame_prefixes)
    return {
        "packet_count": len(packets),
        "sensor_packet_count": len(sensor_packets),
        "sensor_descriptor_found": bool(descriptor_addresses),
        "sensor_addresses": [
            {"bus": bus, "device": device}
            for bus, device in sorted(sensor_addresses)
        ],
        "urb_pairing": _pair_urbs(sensor_packets),
        "endpoint_counts": dict(sorted(endpoint_counts.items())),
        "out_command_signatures": dict(sorted(commands.items())),
        "in_payload_lengths": {str(key): value for key, value in sorted(response_lengths.items())},
        "candidate_frame_count": len(frames),
        "candidate_boundary_confirmed": bool(frames),
        "fragmented_frame_count": sum(
            event["kind"] == "raw-frame" and "fragment_lengths" in event
            for event in protocol_events),
        "orphan_frame_prefix_count": orphan_frame_prefixes,
        "orphan_frame_suffix_count": orphan_frame_suffixes,
        "calibration_upload_count": len(calibration),
        "calibration_uploads": [_payload_summary(payload) for payload in calibration],
        "unlinked_large_transfer_count": len(unlinked_large),
        "protocol_events": protocol_events,
        "traffic_runs": traffic_runs,
        "protocol_traffic_present": bool(commands),
        "usable_sensor_payload_present": bool(frames or calibration),
        "warnings": warnings,
    }, frames


def _load_timeline(path):
    path = Path(path)
    if not path.is_file():
        return []
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("capture timeline exceeds size budget")
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if set(item) != {"capture", "phase", "started", "stopped", "instruction"}:
                raise ValueError
            started = datetime.fromisoformat(item["started"]).timestamp()
            stopped = datetime.fromisoformat(item["stopped"]).timestamp()
            if stopped < started:
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid timeline record at line {line_number}") from error
        records.append((item, started, stopped))
    return records


def write_capture_analysis(captures, output, tshark="tshark"):
    output = Path(output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    output.chmod(0o700)
    captures = [Path(capture) for capture in captures]
    parents = {capture.parent.resolve() for capture in captures}
    timeline = _load_timeline(next(iter(parents)) / "timeline.jsonl") if len(parents) == 1 else []
    phases = []
    for capture in captures:
        packets = decode_usbpcap(capture, tshark=tshark)
        report, frames = analyze_packets(packets)
        frame_file = output / f"{Path(capture).stem}.frames.bin"
        frame_file.write_bytes(b"".join(frames))
        frame_file.chmod(0o600)
        timed_phases = []
        for item, started, stopped in timeline:
            if item["capture"] != capture.stem:
                continue
            selected = [packet for packet in packets
                        if started <= packet.time_epoch <= stopped]
            phase_report, phase_frames = analyze_packets(selected)
            phase_file = output / f"{capture.stem}.{item['phase']}.frames.bin"
            phase_file.write_bytes(b"".join(phase_frames))
            phase_file.chmod(0o600)
            expects_biometric_traffic = item["phase"].startswith(
                ("genuine-", "impostor-", "temporary-enrollment"))
            phase_capture_valid = (
                phase_report["protocol_traffic_present"]
                and bool(phase_frames or phase_report["unlinked_large_transfer_count"])
            ) if expects_biometric_traffic else True
            timed_phases.append({
                "phase": item["phase"], "instruction": item["instruction"],
                "frames_file": phase_file.name,
                "expects_biometric_traffic": expects_biometric_traffic,
                "phase_capture_valid": phase_capture_valid,
                **phase_report,
            })
        failed_timeline_phases = [
            phase["phase"] for phase in timed_phases
            if phase["expects_biometric_traffic"] and not phase["phase_capture_valid"]
        ]
        phases.append({
            "phase": Path(capture).stem,
            "capture_sha256": hashlib.sha256(Path(capture).read_bytes()).hexdigest(),
            "frames_file": frame_file.name,
            "timeline_phases": timed_phases,
            "timeline_capture_valid": not failed_timeline_phases,
            "failed_timeline_phases": failed_timeline_phases,
            **report,
        })
    result = {"schema_version": 2, "private": True, "frame_geometry": [103, 52], "phases": phases}
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
