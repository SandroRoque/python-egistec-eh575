import json
import tempfile
import unittest
from pathlib import Path

from egis_driver.windows_capture import analyze_packets, UsbPacket, write_capture_analysis


class WindowsCaptureTests(unittest.TestCase):
    def test_packet_analysis_never_copies_image_payload_into_report(self):
        frame = bytes(range(256)) * 20 + bytes(236)
        report, frames = analyze_packets([
            UsbPacket(1, 1.0, 0x01, "3", bytes.fromhex("454749536414ec")),
            UsbPacket(2, 1.1, 0x82, "3", frame),
        ])
        self.assertEqual(report["candidate_frame_count"], 1)
        self.assertFalse(report["candidate_boundary_confirmed"])
        self.assertEqual(frames, [frame])
        self.assertNotIn(frame.hex(), json.dumps(report))
        self.assertEqual(report["traffic_runs"], [
            {"kind": "out:454749536414ec", "count": 1,
             "first_offset_ms": 0.0, "last_offset_ms": 0.0},
            {"kind": "in:image-candidate:5356", "count": 1,
             "first_offset_ms": 100.0, "last_offset_ms": 100.0},
        ])

    def test_packet_analysis_run_length_encodes_status_timeline(self):
        report, _ = analyze_packets([
            UsbPacket(4, 2.0, 0x82, "3", b"\x01\x02"),
            UsbPacket(5, 2.1, 0x82, "3", b"\x01\x02"),
        ])
        self.assertEqual(report["traffic_runs"], [{
            "kind": "in:response:2:0102", "count": 2,
            "first_offset_ms": 0.0, "last_offset_ms": 100.0,
        }])

    def test_capture_analysis_writes_private_frame_payload_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "idle.pcapng"
            capture.write_bytes(b"capture")
            output = root / "result"
            import egis_driver.windows_capture as module
            original = module.decode_usbpcap
            module.decode_usbpcap = lambda *args, **kwargs: [
                UsbPacket(1, 1.0, 0x82, "3", bytes(5356))]
            try:
                report = write_capture_analysis([capture], output)
            finally:
                module.decode_usbpcap = original
            self.assertEqual(report["phases"][0]["candidate_frame_count"], 1)
            self.assertEqual((output / "idle.frames.bin").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
