import importlib
import json
import os
import platform
import re
import subprocess
from pathlib import Path

from egis_driver.device_profile import EH575_PROFILE
from egis_driver.fingerprint_matcher import MATCHER_VERSION, TEMPLATE_SCHEMA_VERSION
from egis_driver.runtime_config import RuntimePaths
from egis_driver.version import __version__


DEPENDENCIES = {
    "opencv": "cv2",
    "numpy": "numpy",
    "scikit_image": "skimage",
    "pyusb": "usb",
}


def _read(path, base=10):
    value = Path(path).read_text(encoding="ascii").strip()
    return int(value, base) if base else value


def _os_release(path="/etc/os-release"):
    values = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    except OSError:
        pass
    return {
        "id": values.get("ID", "unknown"),
        "version_id": values.get("VERSION_ID", values.get("BUILD_ID", "unknown")),
    }


def dependency_versions():
    versions = {}
    for name, module_name in DEPENDENCIES.items():
        try:
            module = importlib.import_module(module_name)
            versions[name] = str(getattr(module, "__version__", "available"))
        except Exception:
            versions[name] = "missing"
    return versions


def platform_report():
    return {
        "distribution": _os_release(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "dependencies": dependency_versions(),
    }


def _interface_report(interface):
    endpoints = []
    for endpoint in sorted(interface.glob("ep_*"), key=lambda item: item.name):
        try:
            endpoints.append({
                "address": f"{_read(endpoint / 'bEndpointAddress', 16):02x}",
                "attributes": f"{_read(endpoint / 'bmAttributes', 16):02x}",
                "max_packet_size": _read(endpoint / "wMaxPacketSize", 16),
            })
        except OSError:
            continue
    return {
        "number": f"{_read(interface / 'bInterfaceNumber', 16):02x}",
        "class": f"{_read(interface / 'bInterfaceClass', 16):02x}",
        "subclass": f"{_read(interface / 'bInterfaceSubClass', 16):02x}",
        "protocol": f"{_read(interface / 'bInterfaceProtocol', 16):02x}",
        "endpoints": endpoints,
    }


def usb_report(sysfs_root="/sys/bus/usb/devices", profile=EH575_PROFILE):
    root = Path(sysfs_root)
    matches = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return {
            "status": "unavailable",
            "compatible": False,
            "warnings": ["USB sysfs is unavailable"],
            "devices": [],
        }

    for entry in entries:
        if ":" in entry.name or not (entry / "idVendor").is_file():
            continue
        try:
            vendor = _read(entry / "idVendor", 16)
            product = _read(entry / "idProduct", 16)
        except OSError:
            continue
        if (vendor, product) != (profile.vendor_id, profile.product_id):
            continue

        resolved = entry.resolve()
        interfaces = []
        for candidate in sorted(resolved.glob(f"{entry.name}:*")):
            if (candidate / "bInterfaceNumber").is_file():
                interfaces.append(_interface_report(candidate))
        try:
            speed = float(_read(entry / "speed", 0))
            speed = int(speed) if speed.is_integer() else speed
        except (OSError, ValueError):
            speed = None
        matches.append({
            "usb_id": profile.usb_id,
            "bcd_device": f"{_read(entry / 'bcdDevice', 16):04x}",
            "usb_version": _read(entry / "version", 0).strip(),
            "speed_mbps": speed,
            "interfaces": interfaces,
        })

    warnings = []
    compatible = len(matches) == 1
    status = "compatible" if compatible else "incompatible"
    if not matches:
        warnings.append(f"{profile.name} ({profile.usb_id}) was not found")
    elif len(matches) > 1:
        warnings.append("Multiple matching EH575 devices require explicit selection")
    else:
        device = matches[0]
        if device["bcd_device"] not in profile.known_revisions:
            warnings.append(f"Untested device revision {device['bcd_device']}")
        expected_identity = (
            f"{profile.interface_number:02x}",
            f"{profile.interface_class:02x}",
            f"{profile.interface_subclass:02x}",
            f"{profile.interface_protocol:02x}",
        )
        interface = next((
            item for item in device["interfaces"]
            if (
                item["number"], item["class"], item["subclass"], item["protocol"]
            ) == expected_identity
        ), None)
        if interface is None:
            compatible = False
            warnings.append("Required vendor-specific interface ff/ff/00 is missing")
        else:
            endpoints = {item["address"]: item for item in interface["endpoints"]}
            for address in (profile.endpoint_out, profile.endpoint_in):
                key = f"{address:02x}"
                endpoint = endpoints.get(key)
                if endpoint is None:
                    compatible = False
                    warnings.append(f"Required endpoint {key} is missing")
                elif endpoint["max_packet_size"] < profile.endpoint_packet_size:
                    compatible = False
                    warnings.append(f"Endpoint {key} packet size is too small")
        status = "compatible" if compatible else "incompatible"
    return {
        "status": status,
        "compatible": compatible,
        "warnings": warnings,
        "devices": matches,
    }


def _command_status(command, success_text=None):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if result.returncode != 0:
        return "failed"
    if success_text and success_text not in result.stdout:
        return "failed"
    return "ready"


def service_report():
    manager = _command_status(["systemctl", "is-active", "open-fprintd"])
    bridge = _command_status(["systemctl", "is-active", "egis-bridge"])
    device = _command_status(
        [
            "busctl", "--system", "call", "net.reactivated.Fprint",
            "/net/reactivated/Fprint/Manager",
            "net.reactivated.Fprint.Manager", "GetDefaultDevice",
        ],
        "/net/reactivated/Fprint/Device/",
    )
    return {
        "open_fprintd": manager,
        "egis_bridge": bridge,
        "dbus_device": device,
        "ready": all(value == "ready" for value in (manager, bridge, device)),
    }


def _state_entry(path):
    try:
        stat = path.stat()
    except OSError:
        return {"status": "unavailable"}
    return {
        "status": "present",
        "mode": f"{stat.st_mode & 0o777:04o}",
        "uid": stat.st_uid,
        "gid": stat.st_gid,
    }


def state_report(runtime_paths=None):
    paths = runtime_paths or RuntimePaths.from_environment()
    calibration = "unavailable"
    threshold_file = paths.calibration_dir / "thresholds.json"
    try:
        thresholds = json.loads(threshold_file.read_text(encoding="utf-8"))
        calibration = (
            "validated"
            if thresholds.get("validated") is True and
            thresholds.get("matcher_version") == MATCHER_VERSION
            else "invalid"
        )
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "enrollment": _state_entry(paths.enrollment_dir),
        "calibration": _state_entry(paths.calibration_dir),
        "thresholds": calibration,
    }


def source_identity(root=None):
    root = Path(root or Path(__file__).resolve().parents[2])
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"commit": None, "dirty": None}
    value = commit.stdout.strip()
    if commit.returncode != 0 or len(value) != 40:
        return {"commit": None, "dirty": None}
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return {"commit": value, "dirty": bool(dirty.stdout.strip())}


def doctor_report(sysfs_root="/sys/bus/usb/devices", check_services=True):
    hardware = usb_report(sysfs_root=sysfs_root)
    services = service_report() if check_services else {"ready": None}
    source = source_identity()
    return {
        "schema_version": 1,
        "project": {
            "name": "open-fprintd-eh575",
            "version": __version__,
            "source_commit": source["commit"],
            "source_dirty": source["dirty"],
            "matcher_version": MATCHER_VERSION,
            "template_schema_version": TEMPLATE_SCHEMA_VERSION,
        },
        "platform": platform_report(),
        "hardware": hardware,
        "services": services,
        "state": state_report(),
        "compatible": bool(
            hardware["compatible"] and
            (services["ready"] in (True, None))
        ),
    }


def _public_platform(platform_data):
    distribution = platform_data.get("distribution", {})
    dependencies = platform_data.get("dependencies", {})
    return {
        "distribution": {
            "id": str(distribution.get("id", "unknown")),
            "version_id": str(distribution.get("version_id", "unknown")),
        },
        "kernel": str(platform_data.get("kernel", "unknown")),
        "architecture": str(platform_data.get("architecture", "unknown")),
        "python": str(platform_data.get("python", "unknown")),
        "dependencies": {
            key: str(dependencies.get(key, "missing"))
            for key in DEPENDENCIES
        },
    }


def _public_hardware(hardware_data):
    devices = []
    for device in hardware_data.get("devices", []):
        interfaces = []
        for interface in device.get("interfaces", []):
            endpoints = [
                {
                    "address": str(endpoint.get("address", "")),
                    "attributes": str(endpoint.get("attributes", "")),
                    "max_packet_size": int(endpoint.get("max_packet_size", 0)),
                }
                for endpoint in interface.get("endpoints", [])
            ]
            interfaces.append({
                "number": str(interface.get("number", "")),
                "class": str(interface.get("class", "")),
                "subclass": str(interface.get("subclass", "")),
                "protocol": str(interface.get("protocol", "")),
                "endpoints": endpoints,
            })
        devices.append({
            "usb_id": str(device.get("usb_id", "")),
            "bcd_device": str(device.get("bcd_device", "")),
            "usb_version": str(device.get("usb_version", "")),
            "speed_mbps": device.get("speed_mbps"),
            "interfaces": interfaces,
        })
    warning_codes = []
    for warning in hardware_data.get("warnings", []):
        if warning == "USB sysfs is unavailable":
            warning_codes.append("usb_sysfs_unavailable")
        elif warning.endswith("was not found"):
            warning_codes.append("device_not_found")
        elif warning.startswith("Multiple matching EH575 devices"):
            warning_codes.append("multiple_devices")
        elif match := re.fullmatch(r"Untested device revision ([0-9a-fA-F]{4})", warning):
            warning_codes.append(f"untested_revision:{match.group(1).lower()}")
        elif warning.startswith("Required vendor-specific interface"):
            warning_codes.append("interface_mismatch")
        elif match := re.fullmatch(r"Required endpoint ([0-9a-fA-F]{2}) is missing", warning):
            warning_codes.append(f"endpoint_missing:{match.group(1).lower()}")
        elif warning.startswith("Endpoint ") and warning.endswith("packet size is too small"):
            warning_codes.append("endpoint_packet_size")
        else:
            warning_codes.append("unspecified_hardware_warning")
    return {
        "status": str(hardware_data.get("status", "unavailable")),
        "compatible": bool(hardware_data.get("compatible", False)),
        "warnings": warning_codes,
        "devices": devices,
    }


def public_compatibility_report(evaluation, lifecycle, environment):
    if evaluation.get("schema_version") != 1:
        raise ValueError("unsupported evaluation schema")
    if lifecycle.get("schema_version") != 1:
        raise ValueError("unsupported lifecycle schema")

    required = {
        "service_restarts": 3,
        "suspend_resume": 5,
    }
    for key, minimum in required.items():
        result = lifecycle.get(key, {})
        if int(result.get("attempts", 0)) < minimum:
            raise ValueError(f"{key} requires at least {minimum} attempts")
        if int(result.get("passed", 0)) != int(result.get("attempts", 0)):
            raise ValueError(f"{key} did not pass every attempt")
    idle = lifecycle.get("locked_idle", {})
    if int(idle.get("minutes", 0)) < 30 or int(idle.get("false_failures", -1)) != 0:
        raise ValueError("locked_idle requires 30 minutes with zero false failures")
    for key in ("password_fallback", "fprintd_clients"):
        if lifecycle.get(key) != "pass":
            raise ValueError(f"{key} must be 'pass'")

    targets = {}
    target_gates = {}
    original_gates = evaluation.get("gates", {}).get("targets", {})
    for index, (original_key, result) in enumerate(
        sorted(evaluation.get("targets", {}).items()),
        1,
    ):
        public_key = f"target_{index}"
        targets[public_key] = {
            key: result[key]
            for key in (
                "genuine_total", "genuine_pass", "genuine_pass_required",
                "impostor_total", "impostor_accept",
            )
        }
        target_gates[public_key] = bool(original_gates.get(original_key, False))
    sanitized_lifecycle = {
        "schema_version": 1,
        "service_restarts": {
            "attempts": int(lifecycle["service_restarts"]["attempts"]),
            "passed": int(lifecycle["service_restarts"]["passed"]),
        },
        "suspend_resume": {
            "attempts": int(lifecycle["suspend_resume"]["attempts"]),
            "passed": int(lifecycle["suspend_resume"]["passed"]),
        },
        "locked_idle": {
            "minutes": int(lifecycle["locked_idle"]["minutes"]),
            "false_failures": int(lifecycle["locked_idle"]["false_failures"]),
        },
        "password_fallback": lifecycle["password_fallback"],
        "fprintd_clients": lifecycle["fprintd_clients"],
    }
    project = {
        key: environment["project"].get(key)
        for key in (
            "name", "version", "source_commit", "source_dirty",
            "matcher_version", "template_schema_version",
        )
    }
    report = {
        "schema_version": 1,
        "project": project,
        "platform": _public_platform(environment.get("platform", {})),
        "hardware": _public_hardware(environment.get("hardware", {})),
        "evaluation": {
            "role": evaluation.get("dataset", {}).get("role"),
            "repeats": evaluation.get("repeats"),
            "deterministic": evaluation.get("deterministic"),
            "targets": targets,
            "latency": evaluation.get("latency"),
            "gates": {
                "deterministic": bool(evaluation.get("gates", {}).get("deterministic")),
                "latency": bool(evaluation.get("gates", {}).get("latency")),
                "passed": bool(evaluation.get("gates", {}).get("passed")),
                "targets": target_gates,
            },
        },
        "lifecycle": sanitized_lifecycle,
    }
    report["passed"] = bool(
        environment.get("compatible") and
        evaluation.get("gates", {}).get("passed")
    )
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    import hashlib
    report["report_id"] = hashlib.sha256(encoded).hexdigest()[:16]
    return report
