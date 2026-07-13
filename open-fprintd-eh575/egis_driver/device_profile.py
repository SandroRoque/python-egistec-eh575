from dataclasses import dataclass


@dataclass(frozen=True)
class FrameSpec:
    width: int
    height: int
    dtype: str = "uint8"

    @property
    def byte_count(self):
        return self.width * self.height


@dataclass(frozen=True)
class DeviceProfile:
    name: str
    vendor_id: int
    product_id: int
    interface_number: int
    interface_class: int
    interface_subclass: int
    interface_protocol: int
    endpoint_out: int
    endpoint_in: int
    endpoint_packet_size: int
    frame: FrameSpec
    touch_threshold: float
    known_revisions: tuple[str, ...]
    patch_commands: tuple[str, ...]
    init_commands: tuple[str, ...]
    final_commands: tuple[str, ...]
    rearm_commands: tuple[str, ...]
    trigger_command: str

    @property
    def usb_id(self):
        return f"{self.vendor_id:04x}:{self.product_id:04x}"


EH575_FRAME = FrameSpec(width=103, height=52)

EH575_PROFILE = DeviceProfile(
    name="EgisTec EH575",
    vendor_id=0x1C7A,
    product_id=0x0575,
    interface_number=0,
    interface_class=0xFF,
    interface_subclass=0xFF,
    interface_protocol=0x00,
    endpoint_out=0x01,
    endpoint_in=0x82,
    endpoint_packet_size=512,
    frame=EH575_FRAME,
    touch_threshold=31.0,
    known_revisions=("1072",),
    patch_commands=(
        "45 47 49 53 60 00 06", "45 47 49 53 60 01 06",
        "45 47 49 53 60 40 06", "45 47 49 53 61 0a f4",
        "45 47 49 53 61 0c 44", "45 47 49 53 61 40 00",
        "45 47 49 53 60 40 00", "45 47 49 53 71 02 02 01 0c",
        "45 47 49 53 61 0c 22", "45 47 49 53 61 0b 03",
        "45 47 49 53 61 0a fc", "45 47 49 53 60 00 fc",
        "45 47 49 53 60 01 fc", "45 47 49 53 60 41 fc",
    ),
    init_commands=(
        "45 47 49 53 97 00 00",
        "45 47 49 53 60 00 00", "45 47 49 53 60 00 00",
        "45 47 49 53 60 00 00", "45 47 49 53 60 00 00",
        "45 47 49 53 60 00 00", "45 47 49 53 60 01 00",
        "45 47 49 53 61 0a fd", "45 47 49 53 61 35 02",
        "45 47 49 53 61 80 00", "45 47 49 53 60 80 00",
        "45 47 49 53 61 0a fc", "45 47 49 53 63 01 02 0f 03",
        "45 47 49 53 61 0c 22", "45 47 49 53 61 09 83",
        "45 47 49 53 63 26 06 06 60 06 05 2f 06",
        "45 47 49 53 61 0a f4", "45 47 49 53 61 0c 44",
        "45 47 49 53 61 50 03", "45 47 49 53 60 50 03",
    ),
    final_commands=(
        "45 47 49 53 60 40 ec", "45 47 49 53 61 0c 22",
        "45 47 49 53 61 0b 03", "45 47 49 53 61 0a fc",
        "45 47 49 53 60 40 fc",
        "45 47 49 53 63 09 0b 83 24 00 44 0f 08 20 20 01 05 12",
        "45 47 49 53 63 26 06 06 60 06 05 2f 06",
        "45 47 49 53 61 23 00", "45 47 49 53 61 24 33",
        "45 47 49 53 61 20 00", "45 47 49 53 61 21 66",
        "45 47 49 53 60 00 66", "45 47 49 53 60 01 66",
    ),
    rearm_commands=(
        "45 47 49 53 61 2d 20", "45 47 49 53 60 00 20",
        "45 47 49 53 60 01 20", "45 47 49 53 63 2c 02 00 57",
        "45 47 49 53 60 2d 02", "45 47 49 53 62 67 03",
        "45 47 49 53 63 2c 02 00 13", "45 47 49 53 60 00 02",
    ),
    trigger_command="45 47 49 53 64 14 ec",
)
