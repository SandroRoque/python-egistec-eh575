import usb.core
import usb.util
import logging
import os
import time
import numpy as np
import threading

# --- Hardware Constants ---
VENDOR_ID = 0x1c7a
PRODUCT_ID = 0x0575
ENDPOINT_OUT = 0x01
ENDPOINT_IN = 0x82
IMG_WIDTH = 103
IMG_HEIGHT = 52
USB_SYSFS_ROOT = "/sys/bus/usb/devices"

logger = logging.getLogger("DRIVER")

class EgisDriver:
    def __init__(self):
        self._usb_lock = threading.RLock()
        self.dev = self._find_device()
        self.touch_threshold = 31.0
        self._last_iok = 0
        self._reconnect_delay = 10
        self._released_for_sleep = False
        self._initialize_sensor()

    def _find_device(self):
        dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
        if not dev:
            raise ValueError("Egis Sensor not found!")

        if dev.is_kernel_driver_active(0):
            try: dev.detach_kernel_driver(0)
            except: pass

        dev.set_configuration()
        return dev

    def _send_hex(self, hex_str, read_resp=True, timeout_ms=1000):
        cmd = bytes.fromhex(hex_str)
        try:
            self.dev.write(ENDPOINT_OUT, cmd)
            if read_resp:
                resp = self.dev.read(ENDPOINT_IN, 64, timeout=timeout_ms)
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

        patches = [
            "45 47 49 53 60 00 06", "45 47 49 53 60 01 06", "45 47 49 53 60 40 06",
            "45 47 49 53 61 0a f4", "45 47 49 53 61 0c 44", "45 47 49 53 61 40 00",
            "45 47 49 53 60 40 00", "45 47 49 53 71 02 02 01 0c", "45 47 49 53 61 0c 22",
            "45 47 49 53 61 0b 03", "45 47 49 53 61 0a fc"
        ]
        for p in patches: _counted_send(p)

        _counted_send("45 47 49 53 60 00 fc")
        _counted_send("45 47 49 53 60 01 fc")
        _counted_send("45 47 49 53 60 41 fc")

        init_cmds = [
            "45 47 49 53 97 00 00",
            "45 47 49 53 60 00 00", "45 47 49 53 60 00 00", "45 47 49 53 60 00 00",
            "45 47 49 53 60 00 00", "45 47 49 53 60 00 00",
            "45 47 49 53 60 01 00", "45 47 49 53 61 0a fd", "45 47 49 53 61 35 02",
            "45 47 49 53 61 80 00", "45 47 49 53 60 80 00", "45 47 49 53 61 0a fc",
            "45 47 49 53 63 01 02 0f 03", "45 47 49 53 61 0c 22", "45 47 49 53 61 09 83",
            "45 47 49 53 63 26 06 06 60 06 05 2f 06", "45 47 49 53 61 0a f4",
            "45 47 49 53 61 0c 44", "45 47 49 53 61 50 03", "45 47 49 53 60 50 03",
        ]
        for c in init_cmds:
            _counted_send(c)
            time.sleep(0.002)

        final_cmds = [
            "45 47 49 53 60 40 ec", "45 47 49 53 61 0c 22", "45 47 49 53 61 0b 03",
            "45 47 49 53 61 0a fc", "45 47 49 53 60 40 fc",
            "45 47 49 53 63 09 0b 83 24 00 44 0f 08 20 20 01 05 12",
            "45 47 49 53 63 26 06 06 60 06 05 2f 06", "45 47 49 53 61 23 00",
            "45 47 49 53 61 24 33", "45 47 49 53 61 20 00", "45 47 49 53 61 21 66",
            "45 47 49 53 60 00 66", "45 47 49 53 60 01 66",
        ]
        for c in final_cmds: _counted_send(c)

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
                self.dev.write(ENDPOINT_OUT, bytes.fromhex("45 47 49 53 64 14 ec"))
                data = self.dev.read(ENDPOINT_IN, 10000, timeout=1500)
                try:
                    self.dev.read(ENDPOINT_IN, 512, timeout=20)
                except Exception:
                    pass
            except usb.core.USBError:
                continue

            if data and len(data) >= 5000:
                arr = np.array(list(data[:IMG_WIDTH * IMG_HEIGHT]), dtype=np.uint8)
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
        # The critical sequence from your working test
        self._send_hex("45 47 49 53 61 2d 20", timeout_ms=200)
        self._send_hex("45 47 49 53 60 00 20", timeout_ms=200)
        self._send_hex("45 47 49 53 60 01 20", timeout_ms=200)
        self._send_hex("45 47 49 53 63 2c 02 00 57", timeout_ms=200)
        self._send_hex("45 47 49 53 60 2d 02", timeout_ms=200)
        self._send_hex("45 47 49 53 62 67 03", timeout_ms=200)
        self._send_hex("45 47 49 53 63 2c 02 00 13", timeout_ms=200)
        self._send_hex("45 47 49 53 60 00 02", timeout_ms=200)

    def _dispose_device(self):
        try:
            usb.util.dispose_resources(self.dev)
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

            if vendor == f"{VENDOR_ID:04x}" and product == f"{PRODUCT_ID:04x}":
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

    def _ensure_connected(self, force=False, reset=False):
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
        return self._ensure_connected(force=True, reset=reset)

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
                self.dev.write(ENDPOINT_OUT, bytes.fromhex("45 47 49 53 64 14 ec"))

                # 2. The read logic stays inside the try block
                data = self.dev.read(ENDPOINT_IN, 10000, timeout=read_timeout)

                # Drain pipe
                try: self.dev.read(ENDPOINT_IN, 512, timeout=20)
                except: pass

                if len(data) > 5000:
                    target = IMG_WIDTH * IMG_HEIGHT
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
                self._ensure_connected(force=True)

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
            self._ensure_connected()
            return True
