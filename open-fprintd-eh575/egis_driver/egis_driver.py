import usb.core
import usb.util
import logging
import os
import time
import numpy as np
import threading

from egis_driver.device_profile import EH575_PROFILE

# Compatibility aliases for callers that imported the original constants.
VENDOR_ID = EH575_PROFILE.vendor_id
PRODUCT_ID = EH575_PROFILE.product_id
ENDPOINT_OUT = EH575_PROFILE.endpoint_out
ENDPOINT_IN = EH575_PROFILE.endpoint_in
IMG_WIDTH = EH575_PROFILE.frame.width
IMG_HEIGHT = EH575_PROFILE.frame.height
USB_SYSFS_ROOT = "/sys/bus/usb/devices"

logger = logging.getLogger("DRIVER")

class EgisDriver:
    def __init__(self, profile=EH575_PROFILE, usb_core=None, usb_util=None):
        self.profile = profile
        self.frame_spec = profile.frame
        self._usb_core = usb_core or usb.core
        self._usb_util = usb_util or usb.util
        self._usb_lock = threading.RLock()
        self.dev = self._find_device()
        self.touch_threshold = profile.touch_threshold
        self._last_iok = 0
        self._reconnect_delay = 10
        self._released_for_sleep = False
        self._initialize_sensor()

    def _find_device(self):
        devices = list(self._usb_core.find(
            find_all=True,
            idVendor=self.profile.vendor_id,
            idProduct=self.profile.product_id,
        ) or [])
        if not devices:
            raise ValueError(f"{self.profile.name} ({self.profile.usb_id}) not found")
        if len(devices) > 1:
            raise RuntimeError(
                f"Multiple {self.profile.name} devices found; explicit selection is required"
            )
        dev = devices[0]

        if dev.is_kernel_driver_active(self.profile.interface_number):
            try: dev.detach_kernel_driver(self.profile.interface_number)
            except: pass

        dev.set_configuration()
        self._validate_device(dev)
        return dev

    def _validate_device(self, dev):
        revision = f"{int(getattr(dev, 'bcdDevice', 0)):04x}"
        if revision not in self.profile.known_revisions:
            logger.warning(
                "Untested %s revision %s; validating descriptors before use",
                self.profile.name,
                revision,
            )

        configuration = dev.get_active_configuration()
        interface = configuration[(self.profile.interface_number, 0)]
        identity = (
            int(interface.bInterfaceClass),
            int(interface.bInterfaceSubClass),
            int(interface.bInterfaceProtocol),
        )
        expected = (
            self.profile.interface_class,
            self.profile.interface_subclass,
            self.profile.interface_protocol,
        )
        if identity != expected:
            raise RuntimeError(
                f"Unsupported {self.profile.name} interface {identity}; expected {expected}"
            )

        endpoints = {int(endpoint.bEndpointAddress): endpoint for endpoint in interface}
        missing = {
            self.profile.endpoint_out,
            self.profile.endpoint_in,
        } - set(endpoints)
        if missing:
            formatted = ", ".join(f"0x{address:02x}" for address in sorted(missing))
            raise RuntimeError(f"{self.profile.name} is missing required endpoints: {formatted}")
        for address in (self.profile.endpoint_out, self.profile.endpoint_in):
            packet_size = int(endpoints[address].wMaxPacketSize)
            if packet_size < self.profile.endpoint_packet_size:
                raise RuntimeError(
                    f"Endpoint 0x{address:02x} packet size {packet_size} is smaller "
                    f"than required {self.profile.endpoint_packet_size}"
                )

    def _send_hex(self, hex_str, read_resp=True, timeout_ms=1000):
        cmd = bytes.fromhex(hex_str)
        try:
            self.dev.write(self.profile.endpoint_out, cmd)
            if read_resp:
                resp = self.dev.read(self.profile.endpoint_in, 64, timeout=timeout_ms)
                # Validate EGIS/SIGE response magic when response is long enough.
                # The Windows driver checks for 0x45474953 ("SIGE" little-endian)
                # at the start of every 7-byte response. We check for the
                # reversed magic "SIGE" (53 49 47 45) which is what the device
                # sends back on its IN endpoint.
                if len(resp) >= 4:
                    magic = bytes(resp[:4])
                    if magic != b'SIGE' and magic != b'EGIS':
                        logger.debug(
                            "Unexpected response magic: %s (cmd=%s, len=%d)",
                            magic.hex(), hex_str[:20], len(resp),
                        )
                return resp
        except usb.core.USBError:
            pass
        return None

    def _initialize_sensor(self):
        read_timeout = 500
        logger.info("Initializing Hardware (timeout=%dms)...", read_timeout)
        ok_responses = 0
        null_responses = 0
        bad_responses = 0
        total_commands = 0

        def _counted_send(hex_str):
            nonlocal ok_responses, null_responses, bad_responses, total_commands
            total_commands += 1
            resp = self._send_hex(hex_str, timeout_ms=read_timeout)
            if resp is None:
                null_responses += 1
            elif len(resp) >= 4 and bytes(resp[:4]) in (b'SIGE', b'EGIS'):
                ok_responses += 1
            else:
                bad_responses += 1
            return resp

        for command in self.profile.patch_commands:
            _counted_send(command)

        for c in self.profile.init_commands:
            _counted_send(c)
            time.sleep(0.002)

        for command in self.profile.final_commands:
            _counted_send(command)

        logger.info(
            "Init command stats: total=%d ok=%d null=%d bad=%d",
            total_commands, ok_responses, null_responses, bad_responses,
        )
        self._mark_iok()

        # Self-test: capture multiple frames to prove the device is producing
        # image data. Flat empty-frame contrast is still useful diagnostic
        # signal, but it can be normal when no finger is present.
        self._self_test()

    def _self_test(self):
        """Verify the sensor is producing frames after initialization."""
        contrasts = []
        for attempt in range(8):
            try:
                self._rearm()
                self.dev.write(
                    self.profile.endpoint_out,
                    bytes.fromhex(self.profile.trigger_command),
                )
                data = self.dev.read(self.profile.endpoint_in, 10000, timeout=1500)
                try:
                    self.dev.read(self.profile.endpoint_in, 512, timeout=20)
                except Exception:
                    pass
            except usb.core.USBError:
                continue

            if data and len(data) >= 5000:
                arr = np.array(list(data[:self.frame_spec.byte_count]), dtype=np.uint8)
                contrasts.append(float(np.std(arr)))
            time.sleep(0.05)

        if len(contrasts) < 3:
            raise RuntimeError(
                f"Self-test failed: only {len(contrasts)} valid frames out of 8 attempts"
            )

        avg = sum(contrasts) / len(contrasts)
        spread = max(contrasts) - min(contrasts)

        logger.info(
            "Self-test: frames=%d contrast_avg=%.1f spread=%.1f values=%s",
            len(contrasts), avg, spread,
            ", ".join(f"{c:.1f}" for c in contrasts),
        )

        if spread < 2.0:
            logger.info(
                "Self-test contrast is flat; treating as empty sensor diagnostic "
                "(avg=%.1f, spread=%.1f)",
                avg, spread,
            )

        logger.info("Hardware Ready (self-test OK).")

    def _rearm(self):
        for command in self.profile.rearm_commands:
            self._send_hex(command, timeout_ms=200)

    def _dispose_device(self):
        try:
            self._usb_util.dispose_resources(self.dev)
        except Exception:
            pass

    def _find_sysfs_device_path(self):
        try:
            device_names = os.listdir(USB_SYSFS_ROOT)
        except OSError as e:
            logger.warning("USB sysfs unavailable: %s", e)
            return None

        for name in device_names:
            if ":" in name:
                continue
            path = os.path.join(USB_SYSFS_ROOT, name)
            try:
                with open(os.path.join(path, "idVendor"), encoding="ascii") as f:
                    vendor = f.read().strip().lower()
                with open(os.path.join(path, "idProduct"), encoding="ascii") as f:
                    product = f.read().strip().lower()
            except OSError:
                continue

            if (
                    vendor == f"{self.profile.vendor_id:04x}" and
                    product == f"{self.profile.product_id:04x}"):
                return path

        return None

    def _write_sysfs(self, path, value):
        with open(path, "w", encoding="ascii") as f:
            f.write(value)

    def _reauthorize_usb_device(self):
        path = self._find_sysfs_device_path()
        if path is None:
            logger.warning("EH575 sysfs device not found for USB reauthorization")
            return False

        authorized = os.path.join(path, "authorized")
        name = os.path.basename(path)
        logger.info("Reauthorizing USB device via sysfs: %s", name)

        try:
            self._write_sysfs(authorized, "0")
            time.sleep(0.35)
            self._write_sysfs(authorized, "1")
            time.sleep(1.0)
            logger.info("USB sysfs reauthorization complete: %s", name)
            return True
        except OSError as e:
            logger.warning("USB sysfs reauthorization failed for %s: %s", name, e)

        driver_path = "/sys/bus/usb/drivers/usb"
        try:
            logger.info("Falling back to USB driver unbind/bind: %s", name)
            self._write_sysfs(os.path.join(driver_path, "unbind"), name)
            time.sleep(0.35)
            self._write_sysfs(os.path.join(driver_path, "bind"), name)
            time.sleep(1.0)
            logger.info("USB driver unbind/bind complete: %s", name)
            return True
        except OSError as e:
            logger.warning("USB driver unbind/bind failed for %s: %s", name, e)
            return False

    def release_for_sleep(self):
        with self._usb_lock:
            logger.info("Releasing USB resources for sleep")
            self._dispose_device()
            self._last_iok = 0
            self._released_for_sleep = True

    def _reconnect(self, reset=False):
        was_released_for_sleep = self._released_for_sleep
        self._released_for_sleep = False
        self._dispose_device()
        reauthorized = False
        if was_released_for_sleep:
            reauthorized = self._reauthorize_usb_device()

        self.dev = self._find_device()
        if reset and not reauthorized:
            try:
                logger.info("Resetting USB device")
                self.dev.reset()
                time.sleep(0.5)
                self.dev = self._find_device()
            except Exception as e:
                logger.warning("USB reset failed: %s", e)
        self._initialize_sensor()
        self._last_iok = time.time()

    def ensure_connected(self, force=False, reset=False):
        with self._usb_lock:
            idle_for = time.time() - self._last_iok
            if self._released_for_sleep:
                logger.info("USB was released for sleep; forcing reconnect")
                force = True
                reset = True

            if not force and idle_for < self._reconnect_delay:
                return True

            if not force:
                try:
                    self.dev.get_active_configuration()
                except Exception:
                    pass
                else:
                    return True

            reason = "forced" if force else "stale"
            reset_label = " with reset" if reset else ""
            logger.info(
                "USB %s%s (idle for %.0fs), reconnecting...",
                reason,
                reset_label,
                idle_for,
            )
            try:
                self._reconnect(reset=reset)
                logger.info("Reconnected successfully.")
                return True
            except Exception as e:
                logger.warning("Reconnect failed: %s", e)
                return False

    def force_reconnect(self, reset=False):
        return self.ensure_connected(force=True, reset=reset)

    def refresh_after_idle(self, idle_seconds=300):
        idle_for = time.time() - self._last_iok
        if self._last_iok <= 0:
            logger.info(
                "USB has no successful I/O marker; refreshing with reconnect"
            )
            return self.force_reconnect()

        logger.info(
            "USB refresh skipped; preserving current sensor state "
            "(idle_for=%.1fs threshold=%ds)",
            idle_for,
            idle_seconds,
        )
        return True

    def _mark_iok(self):
        self._last_iok = time.time()

    def get_live_frame(self, read_timeout=1500):
        """
        Performs ONE atomic capture cycle: Rearm -> Trigger -> Read -> Contrast.
        Returns: (image_data, contrast_value)
        If no data read (USB error), returns (None, 0.0)
        """
        # 1. We move the try block UP to cover the rearm and the write
        with self._usb_lock:
            try:
                self._rearm()
                self.dev.write(
                    self.profile.endpoint_out,
                    bytes.fromhex(self.profile.trigger_command),
                )

                # 2. The read logic stays inside the try block
                data = self.dev.read(self.profile.endpoint_in, 10000, timeout=read_timeout)

                # Drain pipe
                try: self.dev.read(self.profile.endpoint_in, 512, timeout=20)
                except: pass

                if len(data) > 5000:
                    target = self.frame_spec.byte_count
                    if len(data) < target:
                        data += bytes(target - len(data))
                    else:
                        data = data[:target]

                    arr = np.array(list(data), dtype=np.uint8)
                    contrast = np.std(arr)
                    self._mark_iok()
                    return data, contrast

            except usb.core.USBError as e:
                logger.warning("USB Error: %s", e)
                self.ensure_connected(force=True)

        return None, 0.0

    def capture_presence_frame(self, read_timeout=1500):
        """Capture once and return the frame, contrast, and touch decision."""
        img, contrast = self.get_live_frame(read_timeout=read_timeout)
        return img, contrast, img is not None and contrast >= self.touch_threshold

    def check_sensor_clear(self):
        """Returns True if sensor is empty (contrast < threshold)"""
        try:
            _, contrast = self.get_live_frame()
            return contrast < self.touch_threshold
        except Exception:
            self.ensure_connected()
            return True
