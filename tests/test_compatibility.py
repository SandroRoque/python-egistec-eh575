import json
import tempfile
import unittest
from pathlib import Path

from egis_driver.compatibility import public_compatibility_report, usb_report


class UsbCompatibilityTests(unittest.TestCase):
    def _write(self, root, relative, value):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value), encoding="ascii")

    def _device(self, root, revision="1072", endpoints=("01", "82")):
        device = root / "3-7"
        self._write(device, "idVendor", "1c7a")
        self._write(device, "idProduct", "0575")
        self._write(device, "bcdDevice", revision)
        self._write(device, "version", "2.00")
        self._write(device, "speed", "480")
        interface = device / "3-7:1.0"
        self._write(interface, "bInterfaceNumber", "00")
        self._write(interface, "bInterfaceClass", "ff")
        self._write(interface, "bInterfaceSubClass", "ff")
        self._write(interface, "bInterfaceProtocol", "00")
        for address in endpoints:
            endpoint = interface / f"ep_{address}"
            self._write(endpoint, "bEndpointAddress", address)
            self._write(endpoint, "bmAttributes", "02")
            self._write(endpoint, "wMaxPacketSize", "0200")
        return device

    def test_known_descriptor_is_compatible_and_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = self._device(root)
            self._write(device, "serial", "private-serial")

            report = usb_report(root)

        self.assertTrue(report["compatible"])
        encoded = json.dumps(report)
        self.assertNotIn("private-serial", encoded)
        self.assertNotIn("3-7", encoded)

    def test_unknown_revision_warns_without_rejecting_matching_descriptors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._device(root, revision="9999")

            report = usb_report(root)

        self.assertTrue(report["compatible"])
        self.assertEqual(report["warnings"], ["Untested device revision 9999"])

    def test_missing_endpoint_is_incompatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._device(root, endpoints=("01",))

            report = usb_report(root)

        self.assertFalse(report["compatible"])
        self.assertIn("Required endpoint 82 is missing", report["warnings"])


class PublicCompatibilityReportTests(unittest.TestCase):
    def _evaluation(self):
        target = {
            "genuine_total": 8,
            "genuine_pass": 6,
            "genuine_pass_required": 6,
            "impostor_total": 8,
            "impostor_accept": 0,
        }
        return {
            "schema_version": 1,
            "dataset": {"role": "holdout", "root": "/sensitive/raw"},
            "repeats": 5,
            "deterministic": True,
            "targets": {
                "private-user/right-index-finger": target,
                "private-user/right-thumb": target,
            },
            "latency": {"p50_ms": 100, "p95_ms": 150, "max_ms": 200},
            "gates": {
                "deterministic": True,
                "latency": True,
                "targets": {
                    "private-user/right-index-finger": True,
                    "private-user/right-thumb": True,
                },
                "passed": True,
            },
            "records": [{"username": "private-user", "sample": "secret.npz"}],
        }

    def _lifecycle(self):
        return {
            "schema_version": 1,
            "service_restarts": {"attempts": 3, "passed": 3},
            "suspend_resume": {"attempts": 5, "passed": 5},
            "locked_idle": {"minutes": 30, "false_failures": 0},
            "password_fallback": "pass",
            "fprintd_clients": "pass",
            "notes": "private-user on private-host",
        }

    def _environment(self):
        return {
            "project": {
                "name": "open-fprintd-eh575",
                "version": "0.4.0",
                "source_commit": "a" * 40,
                "source_dirty": False,
                "matcher_version": 5,
                "template_schema_version": 4,
            },
            "platform": {
                "distribution": {"id": "arch", "hostname": "private-host"},
                "dependencies": {},
                "private_path": "/sensitive/raw",
            },
            "hardware": {
                "status": "compatible",
                "compatible": True,
                "warnings": ["private-user on private-host"],
                "devices": [{
                    "usb_id": "1c7a:0575",
                    "bcd_device": "1072",
                    "usb_version": "2.00",
                    "speed_mbps": 480,
                    "serial": "private-serial",
                    "interfaces": [],
                }],
            },
            "compatible": True,
        }

    def test_report_contains_only_sanitized_aggregates(self):
        report = public_compatibility_report(
            self._evaluation(),
            self._lifecycle(),
            self._environment(),
        )

        encoded = json.dumps(report, sort_keys=True)
        self.assertTrue(report["passed"])
        self.assertNotIn("private-user", encoded)
        self.assertNotIn("private-host", encoded)
        self.assertNotIn("secret.npz", encoded)
        self.assertNotIn("/sensitive/raw", encoded)
        self.assertNotIn("private-serial", encoded)
        self.assertEqual(set(report["evaluation"]["targets"]), {"target_1", "target_2"})

    def test_incomplete_lifecycle_is_rejected(self):
        lifecycle = self._lifecycle()
        lifecycle["suspend_resume"] = {"attempts": 4, "passed": 4}

        with self.assertRaisesRegex(ValueError, "at least 5"):
            public_compatibility_report(
                self._evaluation(),
                lifecycle,
                self._environment(),
            )


if __name__ == "__main__":
    unittest.main()
