import usb.core
import usb.util
import logging
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
                return self.dev.read(ENDPOINT_IN, 64, timeout=timeout_ms)
        except usb.core.USBError:
            pass
        return None

    def _initialize_sensor(self, fast=False):
        read_timeout = 200 if fast else 500
        logger.info("Initializing Hardware (fast=%s, timeout=%dms)...", fast, read_timeout)
        patches = [
            "45 47 49 53 60 00 06", "45 47 49 53 60 01 06", "45 47 49 53 60 40 06",
            "45 47 49 53 61 0a f4", "45 47 49 53 61 0c 44", "45 47 49 53 61 40 00",
            "45 47 49 53 60 40 00", "45 47 49 53 71 02 02 01 0c", "45 47 49 53 61 0c 22",
            "45 47 49 53 61 0b 03", "45 47 49 53 61 0a fc"
        ]
        for p in patches: self._send_hex(p, timeout_ms=read_timeout)

        self._send_hex("45 47 49 53 60 00 fc", timeout_ms=read_timeout)
        self._send_hex("45 47 49 53 60 01 fc", timeout_ms=read_timeout)
        self._send_hex("45 47 49 53 60 41 fc", timeout_ms=read_timeout)

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
            self._send_hex(c, timeout_ms=read_timeout)
            time.sleep(0.002)

        final_cmds = [
            "45 47 49 53 60 40 ec", "45 47 49 53 61 0c 22", "45 47 49 53 61 0b 03",
            "45 47 49 53 61 0a fc", "45 47 49 53 60 40 fc",
            "45 47 49 53 63 09 0b 83 24 00 44 0f 08 20 20 01 05 12",
            "45 47 49 53 63 26 06 06 60 06 05 2f 06", "45 47 49 53 61 23 00",
            "45 47 49 53 61 24 33", "45 47 49 53 61 20 00", "45 47 49 53 61 21 66",
            "45 47 49 53 60 00 66", "45 47 49 53 60 01 66",
        ]
        for c in final_cmds: self._send_hex(c, timeout_ms=read_timeout)
        self._mark_iok()
        logger.info("Hardware Ready.")

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

    def release_for_sleep(self):
        with self._usb_lock:
            logger.info("Releasing USB resources for sleep")
            self._dispose_device()
            self._last_iok = 0
            self._released_for_sleep = True

    def _reconnect(self, reset=False):
        was_sleeping = self._released_for_sleep
        self._released_for_sleep = False
        self._dispose_device()
        self.dev = self._find_device()
        if reset:
            try:
                logger.info("Resetting USB device")
                self.dev.reset()
                time.sleep(0.25)
                self.dev = self._find_device()
            except Exception as e:
                logger.warning("USB reset failed: %s", e)
        self._initialize_sensor(fast=was_sleeping)
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
        if time.time() - self._last_iok >= idle_seconds:
            return self.force_reconnect()
        try:
            self._initialize_sensor()
            return True
        except Exception as e:
            logger.warning("Refresh failed: %s", e)
            return self.force_reconnect()

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
