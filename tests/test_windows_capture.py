import json
import struct
import tempfile
import unittest
from pathlib import Path

from egis_driver.windows_capture import (
    analyze_packets, UsbPacket, write_capture_analysis, _decode_usbpcap_pcap,
)


class WindowsCaptureTests(unittest.TestCase):
    def fragmented_packets(self, middle=(), suffix_time=1.02, suffix_device=3):
        return [
            UsbPacket(1, 1.0, 0x01, "3", b"EGIS\x64\x14\xec", bus=2, device=3),
            UsbPacket(2, 1.01, 0x82, "3", bytes([17]) * 5120, bus=2, device=3),
            *middle,
            UsbPacket(9, suffix_time, 0x82, "3", bytes([23]) * 236,
                      bus=2, device=suffix_device),
        ]

    def test_windows_fragment_pair_is_reassembled_as_one_frame(self):
        report, frames = analyze_packets(self.fragmented_packets())
        self.assertEqual(frames, [bytes([17]) * 5120 + bytes([23]) * 236])
        self.assertEqual(report["fragmented_frame_count"], 1)
        self.assertEqual(report["orphan_frame_prefix_count"], 0)
        self.assertEqual(report["orphan_frame_suffix_count"], 0)

    def test_windows_fragment_missing_suffix_is_not_a_frame(self):
        report, frames = analyze_packets(self.fragmented_packets()[:-1])
        self.assertEqual(frames, [])
        self.assertEqual(report["orphan_frame_prefix_count"], 1)

    def test_windows_fragments_from_different_addresses_do_not_mix(self):
        other_device = UsbPacket(3, 1.015, 0x01, "3", b"EGIS\x64\x14\xec",
                                 bus=2, device=4)
        report, frames = analyze_packets(self.fragmented_packets(
            middle=[other_device], suffix_device=4))
        self.assertEqual(frames, [])
        self.assertEqual(report["orphan_frame_prefix_count"], 1)
        self.assertEqual(report["orphan_frame_suffix_count"], 1)

    def test_windows_fragment_pair_over_timeout_is_not_a_frame(self):
        report, frames = analyze_packets(self.fragmented_packets(suffix_time=1.2))
        self.assertEqual(frames, [])
        self.assertEqual(report["orphan_frame_prefix_count"], 1)
        self.assertEqual(report["orphan_frame_suffix_count"], 1)

    def test_short_status_between_fragments_invalidates_prefix(self):
        status = UsbPacket(3, 1.015, 0x82, "3", b"SIGE\x00\x00\x01",
                           bus=2, device=3)
        report, frames = analyze_packets(self.fragmented_packets(middle=[status]))
        self.assertEqual(frames, [])
        self.assertEqual(report["orphan_frame_prefix_count"], 1)
        self.assertEqual(report["orphan_frame_suffix_count"], 1)

    def test_repeated_windows_prefix_replaces_incomplete_frame(self):
        replacement = UsbPacket(3, 1.015, 0x82, "3", bytes([19]) * 5120,
                                bus=2, device=3)
        report, frames = analyze_packets(self.fragmented_packets(middle=[replacement]))
        self.assertEqual(frames, [bytes([19]) * 5120 + bytes([23]) * 236])
        self.assertEqual(report["orphan_frame_prefix_count"], 1)

    def test_windows_wizard_automates_capture_lifecycle_and_reenumeration(self):
        script = (Path(__file__).parents[1] / "tools" / "windows-capture.ps1").read_text()
        self.assertIn('"--capture-from-all-devices"', script)
        self.assertIn('"--capture-from-new-devices"', script)
        self.assertIn("Disable-PnpDevice", script)
        self.assertIn("Enable-PnpDevice", script)
        self.assertIn("LockWorkStation", script)
        self.assertIn("Copy-Item", script)
        self.assertNotIn("Read-Host", script)

    def test_packet_analysis_never_copies_image_payload_into_report(self):
        frame = bytes(range(256)) * 20 + bytes(236)
        report, frames = analyze_packets([
            UsbPacket(1, 1.0, 0x01, "3", bytes.fromhex("454749536414ec")),
            UsbPacket(2, 1.1, 0x82, "3", frame),
        ])
        self.assertEqual(report["candidate_frame_count"], 1)
        self.assertTrue(report["candidate_boundary_confirmed"])
        self.assertEqual(frames, [frame])
        self.assertNotIn(frame.hex(), json.dumps(report))
        self.assertEqual(report["traffic_runs"], [
            {"kind": "command:0x64:out", "count": 1,
             "first_offset_ms": 0.0, "last_offset_ms": 0.0},
            {"kind": "raw-frame:in:5356", "count": 1,
             "first_offset_ms": 100.0, "last_offset_ms": 100.0},
        ])

    def test_packet_analysis_run_length_encodes_status_timeline(self):
        report, _ = analyze_packets([
            UsbPacket(4, 2.0, 0x82, "3", b"SIGE\x01\x02\x01"),
            UsbPacket(5, 2.1, 0x82, "3", b"SIGE\x01\x02\x01"),
        ])
        self.assertEqual(report["traffic_runs"], [{
            "kind": "response:7:in", "count": 2,
            "first_offset_ms": 0.0, "last_offset_ms": 100.0,
        }])

    def test_73_out_payload_is_calibration_upload_not_fingerprint_frame(self):
        calibration = bytes([31]) * 5356
        report, frames = analyze_packets([
            UsbPacket(1, 1.0, 0x01, "3", bytes.fromhex("454749537314ec"),
                      bus=1, device=3),
            UsbPacket(2, 1.1, 0x01, "3", calibration, bus=1, device=3),
            UsbPacket(3, 1.2, 0x82, "3", bytes.fromhex("5349474514ec01"),
                      bus=1, device=3),
        ])
        self.assertEqual(frames, [])
        self.assertEqual(report["calibration_upload_count"], 1)
        self.assertEqual(report["candidate_frame_count"], 0)
        self.assertEqual(report["protocol_events"][1]["kind"], "calibration-upload")

    def test_descriptor_identity_filters_other_usb_devices(self):
        descriptor = bytearray(18)
        descriptor[:2] = b"\x12\x01"
        struct.pack_into("<HH", descriptor, 8, 0x1C7A, 0x0575)
        report, _ = analyze_packets([
            UsbPacket(1, 1.0, 0x80, "2", bytes(descriptor), bus=1, device=5),
            UsbPacket(2, 1.1, 0x01, "3", b"unrelated", bus=1, device=1),
            UsbPacket(3, 1.2, 0x01, "3", b"EGIS\x60\x00\x00", bus=1, device=5),
        ])
        self.assertTrue(report["sensor_descriptor_found"])
        self.assertEqual(report["sensor_packet_count"], 2)
        self.assertEqual(report["sensor_addresses"], [{"bus": 1, "device": 5}])

    def test_request_and_completion_records_are_paired_by_irp(self):
        report, _ = analyze_packets([
            UsbPacket(1, 1.0, 0x01, "3", b"EGIS\x60\x00\x00", irp_id=9),
            UsbPacket(2, 1.1, 0x01, "3", b"", irp_id=9, info=1),
            UsbPacket(3, 1.2, 0x82, "3", b"SIGE\x00\x00\x01", irp_id=10,
                      info=1, status=5),
        ])
        self.assertEqual(report["urb_pairing"], {
            "paired": 1, "orphan_requests": 0,
            "orphan_completions": 1, "failed_completions": 1,
        })

    def test_native_usbpcap_decoder_reads_dlt_249_records(self):
        with tempfile.NamedTemporaryFile() as capture:
            capture.write(struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0,
                                      65535, 249))
            header = bytearray(28)
            struct.pack_into("<H", header, 0, 28)
            struct.pack_into("<Q", header, 2, 0x1234)
            header[16] = 1
            struct.pack_into("<HH", header, 17, 2, 7)
            header[21] = 0x82
            header[22] = 3
            payload = b"status"
            struct.pack_into("<I", header, 23, len(payload))
            capture.write(struct.pack("<IIII", 10, 25, 34, 34))
            capture.write(header + payload)
            capture.flush()
            packets = _decode_usbpcap_pcap(capture.name)
        self.assertEqual(packets[0].endpoint, 0x82)
        self.assertEqual(packets[0].payload, payload)
        self.assertEqual((packets[0].bus, packets[0].device), (2, 7))
        self.assertEqual(packets[0].irp_id, 0x1234)
        self.assertTrue(packets[0].is_completion)
        self.assertEqual(packets[0].direction, "in")

    def test_capture_analysis_writes_private_frame_payload_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "idle.pcapng"
            capture.write_bytes(b"capture")
            output = root / "result"
            import egis_driver.windows_capture as module
            original = module.decode_usbpcap
            module.decode_usbpcap = lambda *args, **kwargs: [
                UsbPacket(1, 1.0, 0x01, "3", bytes.fromhex("454749536414ec")),
                UsbPacket(2, 1.1, 0x82, "3", bytes(5356)),
            ]
            try:
                report = write_capture_analysis([capture], output)
            finally:
                module.decode_usbpcap = original
            self.assertEqual(report["phases"][0]["candidate_frame_count"], 1)
            self.assertEqual((output / "idle.frames.bin").stat().st_mode & 0o777, 0o600)

    def test_capture_analysis_segments_continuous_capture_by_timeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "verification.pcap"
            capture.write_bytes(b"capture")
            (root / "timeline.jsonl").write_text(
                json.dumps({
                    "capture": "verification", "phase": "genuine-1",
                    "started": "1970-01-01T00:00:10+00:00",
                    "stopped": "1970-01-01T00:00:12+00:00",
                    "instruction": "right index",
                }) + "\n", encoding="utf-8")
            output = root / "result"
            import egis_driver.windows_capture as module
            original = module.decode_usbpcap
            module.decode_usbpcap = lambda *args, **kwargs: [
                UsbPacket(1, 11.0, 0x01, "3", bytes.fromhex("454749536414ec")),
                UsbPacket(2, 11.1, 0x82, "3", bytes(5356)),
                UsbPacket(3, 20.0, 0x01, "3", b"EGIS\x60\x00\x00"),
            ]
            try:
                report = write_capture_analysis([capture], output)
            finally:
                module.decode_usbpcap = original
            phase = report["phases"][0]["timeline_phases"][0]
            self.assertEqual(phase["phase"], "genuine-1")
            self.assertEqual(phase["candidate_frame_count"], 1)
            self.assertTrue(phase["phase_capture_valid"])
            self.assertTrue(report["phases"][0]["timeline_capture_valid"])

    def test_capture_analysis_rejects_empty_biometric_timeline_phase(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "verification.pcap"
            capture.write_bytes(b"capture")
            (root / "timeline.jsonl").write_text(
                json.dumps({
                    "capture": "verification", "phase": "impostor-right-pinky-1",
                    "started": "1970-01-01T00:00:10+00:00",
                    "stopped": "1970-01-01T00:00:12+00:00",
                    "instruction": "right pinky",
                }) + "\n", encoding="utf-8")
            output = root / "result"
            import egis_driver.windows_capture as module
            original = module.decode_usbpcap
            module.decode_usbpcap = lambda *args, **kwargs: [
                UsbPacket(1, 11.0, 0x01, "3", b"unrelated", bus=1, device=1),
            ]
            try:
                report = write_capture_analysis([capture], output)
            finally:
                module.decode_usbpcap = original
            phase = report["phases"][0]["timeline_phases"][0]
            self.assertFalse(phase["phase_capture_valid"])
            self.assertFalse(report["phases"][0]["timeline_capture_valid"])
            self.assertEqual(report["phases"][0]["failed_timeline_phases"],
                             ["impostor-right-pinky-1"])


if __name__ == "__main__":
    unittest.main()
