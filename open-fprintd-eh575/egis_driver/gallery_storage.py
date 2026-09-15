"""Checksummed production storage for opaque, pixel-free feature galleries."""

import hashlib
import json
import os
from pathlib import Path

from egis_matcher.feature_engine import FeatureRecord
from egis_matcher.gallery import (
    GALLERY_SCHEMA_VERSION, GalleryEntry, GalleryPolicy, PresentationGallery,
)


MAX_GALLERY_BYTES = 32 * 1024 * 1024
MAX_GALLERY_RECORDS = 96


def save_gallery(gallery, directory):
    if not gallery.entries:
        raise ValueError("cannot save an empty feature gallery")
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    directory.chmod(0o700)
    payload = bytearray()
    entries = []
    for item in gallery.entries:
        offset = len(payload)
        payload.extend(item.record.data)
        entries.append({
            "presentation": item.presentation,
            "component": item.component,
            "sequence": item.sequence,
            "representation": item.representation,
            "quality": item.quality,
            "offset": offset,
            "length": len(item.record.data),
        })
    if len(payload) > MAX_GALLERY_BYTES:
        raise ValueError("feature gallery exceeds size budget")
    payload_path = directory / "records.bin"
    with os.fdopen(os.open(payload_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    manifest = {
        "schema_version": GALLERY_SCHEMA_VERSION,
        "identity": gallery.identity,
        "engine": gallery.engine,
        "engine_version": gallery.engine_version,
        "engine_config": gallery.engine_config,
        "policy": gallery.policy.__dict__,
        "presentations": gallery.presentations,
        "extraction_failures": gallery.extraction_failures,
        "contains_raw_frames": False,
        "records": entries,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }
    manifest_path = directory / "manifest.json"
    with os.fdopen(os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    return manifest


def load_gallery(directory):
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    payload_path = directory / "records.bin"
    if (manifest_path.stat().st_size > 1024 * 1024 or
            payload_path.stat().st_size > MAX_GALLERY_BYTES):
        raise ValueError("feature gallery exceeds size budget")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("records", [])
    if (manifest.get("schema_version") != GALLERY_SCHEMA_VERSION or
            manifest.get("contains_raw_frames") is not False or
            not 0 < len(records) <= MAX_GALLERY_RECORDS):
        raise ValueError("unsupported feature gallery")
    payload = payload_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest.get("payload_sha256"):
        raise ValueError("feature gallery checksum mismatch")
    policy = GalleryPolicy(**manifest["policy"])
    if len(records) > policy.max_records_per_identity:
        raise ValueError("feature gallery exceeds configured record count")
    gallery = PresentationGallery(
        manifest["identity"], manifest["engine"], manifest["engine_version"],
        dict(manifest["engine_config"]), policy,
        presentations=int(manifest["presentations"]),
        extraction_failures=int(manifest.get("extraction_failures", 0)))
    expected = 0
    for stored in records:
        offset, length = int(stored["offset"]), int(stored["length"])
        if offset != expected or length <= 0 or offset + length > len(payload):
            raise ValueError("feature gallery record offsets are corrupt")
        quality = float(stored["quality"])
        if (not 0 <= quality <= 1 or
                stored["representation"] not in {"frame", "component"} or
                any(int(stored[name]) < 0 for name in
                    ("presentation", "component", "sequence"))):
            raise ValueError("invalid feature gallery quality")
        record = FeatureRecord(gallery.engine, gallery.engine_version,
                               payload[offset:offset + length])
        gallery.add(GalleryEntry(
            int(stored["presentation"]), int(stored["component"]),
            int(stored["sequence"]), stored["representation"], quality,
            record))
        expected = offset + length
    if expected != len(payload):
        raise ValueError("feature gallery contains trailing data")
    return gallery, manifest
