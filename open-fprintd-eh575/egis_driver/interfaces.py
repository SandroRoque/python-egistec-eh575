from typing import Protocol

from egis_driver.device_profile import FrameSpec


class SensorBackend(Protocol):
    frame_spec: FrameSpec
    touch_threshold: float

    def ensure_connected(self, force=False, reset=False): ...

    def force_reconnect(self, reset=False): ...

    def refresh_after_idle(self, idle_seconds=300): ...

    def release_for_sleep(self): ...

    def get_live_frame(self, read_timeout=1500): ...

    def capture_presence_frame(self, read_timeout=1500): ...
