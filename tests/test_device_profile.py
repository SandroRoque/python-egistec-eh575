import unittest
from dataclasses import FrozenInstanceError
from unittest import mock

import numpy as np

from egis_driver.device_profile import EH575_FRAME, EH575_PROFILE, FrameSpec
import tempfile
from pathlib import Path

from egis_driver.egis_driver import (
    EgisDriver, fit_background_table, load_sensor_background)
from egis_driver.image_features import ImageFeatureExtractor


class FakeEndpoint:
    def __init__(self, address, packet_size=512):
        self.bEndpointAddress = address
        self.wMaxPacketSize = packet_size


class FakeInterface(list):
    bInterfaceClass = 0xFF
    bInterfaceSubClass = 0xFF
    bInterfaceProtocol = 0x00


class FakeConfiguration:
    def __init__(self, endpoints=(0x01, 0x82)):
        self.interface = FakeInterface(FakeEndpoint(address) for address in endpoints)

    def __getitem__(self, key):
        if key != (0, 0):
            raise KeyError(key)
        return self.interface


class FakeDevice:
    def __init__(self, revision=0x1072, endpoints=(0x01, 0x82)):
        self.bcdDevice = revision
        self.configuration = FakeConfiguration(endpoints)
        self.configuration_set = False

    def is_kernel_driver_active(self, interface):
        return False

    def set_configuration(self):
        self.configuration_set = True

    def get_active_configuration(self):
        return self.configuration


class FakeUsbCore:
    def __init__(self, devices):
        self.devices = devices

    def find(self, **kwargs):
        return iter(self.devices)


class FakeUsbUtil:
    @staticmethod
    def dispose_resources(device):
        return None


class DeviceProfileTests(unittest.TestCase):
    def _driver(self, devices):
        with mock.patch.object(EgisDriver, "_initialize_sensor"):
            return EgisDriver(
                usb_core=FakeUsbCore(devices),
                usb_util=FakeUsbUtil(),
            )

    def test_profile_is_immutable_and_has_expected_geometry(self):
        self.assertEqual(EH575_PROFILE.usb_id, "1c7a:0575")
        self.assertEqual(EH575_FRAME.byte_count, 5356)
        with self.assertRaises(FrozenInstanceError):
            EH575_PROFILE.product_id = 0

    def test_feature_extractor_uses_injected_frame_spec(self):
        extractor = ImageFeatureExtractor(frame_spec=FrameSpec(width=3, height=2))

        image = extractor.raw_frame_to_image(bytes(range(6)))

        np.testing.assert_array_equal(
            image,
            np.array([[0, 1, 2], [3, 4, 5]], dtype=np.uint8),
        )

    def test_matching_descriptor_is_accepted(self):
        device = FakeDevice()

        driver = self._driver([device])

        self.assertIs(driver.dev, device)
        self.assertTrue(device.configuration_set)

    def test_unknown_revision_warns_but_matching_descriptor_is_accepted(self):
        with self.assertLogs("DRIVER", level="WARNING") as logs:
            self._driver([FakeDevice(revision=0x9999)])

        self.assertIn("Untested EgisTec EH575 revision 9999", "\n".join(logs.output))

    def test_missing_required_endpoint_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "missing required endpoints: 0x82"):
            self._driver([FakeDevice(endpoints=(0x01,))])

    def test_multiple_devices_require_explicit_selection(self):
        with self.assertRaisesRegex(RuntimeError, "Multiple EgisTec EH575"):
            self._driver([FakeDevice(), FakeDevice()])


class RecordingDevice(FakeDevice):
    """Acknowledges every EGIS command and records the OUT stream."""

    def __init__(self, upload_ack=b"SIGE\x14\xec\x01"):
        super().__init__()
        self.writes = []
        self.upload_ack = upload_ack
        self._pending = b""

    def write(self, endpoint, data):
        data = bytes(data)
        self.writes.append(data)
        if data[:5] == b"EGIS\x73":
            self._pending = None
        elif self._pending is None:
            self._pending = self.upload_ack
        else:
            self._pending = b"SIGE" + data[5:7] + b"\x01"

    def read(self, endpoint, size, timeout=None):
        return self._pending or b"SIGE\x00\x00\x01"


class BackgroundUploadTests(unittest.TestCase):
    def _initialize(self, device, background):
        with mock.patch.object(EgisDriver, "_self_test"), mock.patch("time.sleep"):
            return EgisDriver(usb_core=FakeUsbCore([device]), usb_util=FakeUsbUtil(),
                              background=background)

    def test_upload_matches_windows_position_and_framing(self):
        device = RecordingDevice()
        background = bytes(range(256)) * 20 + bytes(236)
        self._initialize(device, background)
        index = device.writes.index(b"EGIS\x73\x14\xec")
        self.assertEqual(device.writes[index + 1], background)
        self.assertEqual(device.writes[index - 1], bytes.fromhex("45 47 49 53 60 50 03"))
        self.assertEqual(device.writes[index + 2], bytes.fromhex("45 47 49 53 60 40 ec"))

    def test_rejected_upload_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "rejected background upload"):
            self._initialize(RecordingDevice(upload_ack=b"SIGE\x14\xec\x00"), bytes(5356))

    def test_default_driver_sends_no_upload(self):
        device = RecordingDevice()
        self._initialize(device, None)
        self.assertNotIn(b"EGIS\x73\x14\xec", device.writes)

    def test_background_size_is_validated(self):
        with self.assertRaisesRegex(ValueError, "one byte per pixel"):
            EgisDriver(usb_core=FakeUsbCore([FakeDevice()]), usb_util=FakeUsbUtil(),
                       background=b"\0" * 10)


class SensorBackgroundFileTests(unittest.TestCase):
    def test_missing_file_means_no_upload(self):
        self.assertIsNone(load_sensor_background(Path("/nonexistent/egis-bg.bin")))

    def test_valid_file_is_loaded_and_size_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bg.bin"
            path.write_bytes(bytes(5356))
            self.assertEqual(load_sensor_background(path), bytes(5356))
            path.write_bytes(bytes(10))
            with self.assertRaisesRegex(ValueError, "5356 bytes"):
                load_sensor_background(path)


class BackgroundFitTests(unittest.TestCase):
    def test_fit_recovers_per_pixel_table(self):
        rng = np.random.default_rng(3)
        truth = rng.integers(25, 38, size=5356)
        slope = rng.uniform(5.4, 5.6, size=5356)
        levels = (24, 31, 38)
        responses = [102.6 + slope * (level - truth) for level in levels]
        table = np.frombuffer(fit_background_table(levels, responses), np.uint8)
        np.testing.assert_array_equal(table, truth)

    def test_fit_rejects_flat_response(self):
        with self.assertRaisesRegex(RuntimeError, "not increasing"):
            fit_background_table((24, 31), [np.full(4, 50.0), np.full(4, 50.0)])

    def test_calibration_uploads_levels_then_fitted_table(self):
        driver = EgisDriver.__new__(EgisDriver)
        driver.frame_spec = EH575_FRAME
        uploads = []
        offsets = np.arange(5356) % 7

        def initialize():
            uploads.append(driver.background)

        def frame():
            level = driver.background[0]
            data = np.clip(102.6 + 5.5 * (level - 28 - offsets), 0, 255)
            return np.rint(data).astype(np.uint8).tobytes(), 0.0

        driver._initialize_sensor = initialize
        driver.get_live_frame = frame
        table = driver.calibrate_background(frames=2)
        self.assertEqual([upload[0] for upload in uploads[:3]], [24, 31, 38])
        self.assertEqual(uploads[-1], table)
        np.testing.assert_array_equal(np.frombuffer(table, np.uint8), 28 + offsets)


if __name__ == "__main__":
    unittest.main()
