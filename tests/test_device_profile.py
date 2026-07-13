import unittest
from dataclasses import FrozenInstanceError
from unittest import mock

import numpy as np

from egis_driver.device_profile import EH575_FRAME, EH575_PROFILE, FrameSpec
from egis_driver.egis_driver import EgisDriver
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


if __name__ == "__main__":
    unittest.main()
