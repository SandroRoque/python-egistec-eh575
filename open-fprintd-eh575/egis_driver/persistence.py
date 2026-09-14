import json
import logging
import os
import shutil
from pathlib import Path

import numpy as np

logger = logging.getLogger("PERSISTENCE")


class Persistence:
    """Centralized filesystem I/O for templates, calibration data, and samples.

    All paths live under a single root so the caller controls the location.
    """

    def __init__(self, root_dir="/var/lib/open-fprintd"):
        self.root_dir = root_dir
        self.enroll_dir = os.path.join(root_dir, "egis")
        self.atlas_dir = os.path.join(root_dir, "egis-atlas")
        self.calibration_dir = os.path.join(root_dir, "egis-calibration")
        self.sample_dir = os.path.join(self.calibration_dir, "samples")
        self._threshold_file = os.path.join(self.calibration_dir, "thresholds.json")
        self._report_file = os.path.join(self.calibration_dir, "report.json")

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    def ensure_dirs(self):
        os.makedirs(self.enroll_dir, mode=0o700, exist_ok=True)
        os.makedirs(self.atlas_dir, mode=0o700, exist_ok=True)
        os.makedirs(self.calibration_dir, mode=0o700, exist_ok=True)
        os.makedirs(self.sample_dir, mode=0o700, exist_ok=True)

    # ------------------------------------------------------------------
    #  Templates  (enroll_dir)
    # ------------------------------------------------------------------

    def list_template_files(self):
        """Return sorted list of filenames (with extension) in enroll_dir."""
        try:
            return sorted(os.listdir(self.enroll_dir))
        except FileNotFoundError:
            return []

    def load_template_metadata(self, filename):
        """Load the JSON sidecar for a template."""
        safe_name = filename.rsplit(".", 1)[0] if "." in filename else filename
        json_path = os.path.join(self.enroll_dir, safe_name + ".json")
        with open(json_path, "r") as f:
            return json.load(f)

    def load_template_npz(self, filename):
        """Load the .npz data for a template."""
        npz_path = os.path.join(self.enroll_dir, filename)
        return np.load(npz_path, allow_pickle=False)

    def save_template(self, safe_name, npz_data, meta):
        """Persist one enrolled finger as .npz + .json sidecar."""
        base_path = os.path.join(self.enroll_dir, safe_name)
        json_path = base_path + ".json"
        npz_path = base_path + ".npz"

        np.savez(npz_path, **npz_data)
        os.chmod(npz_path, 0o600)

        with open(json_path, "w") as f:
            json.dump(meta, f)
        os.chmod(json_path, 0o600)

    def delete_template_files(self, filenames):
        """Remove a list of template filenames (with their sidecars)."""
        for filename in filenames:
            try:
                os.remove(os.path.join(self.enroll_dir, filename))
            except OSError:
                pass
            safe_name = filename.rsplit(".", 1)[0] if "." in filename else filename
            for ext in (".json",):
                sidecar = safe_name + ext
                if sidecar != filename:
                    try:
                        os.remove(os.path.join(self.enroll_dir, sidecar))
                    except OSError:
                        pass

    def replace_atlas(self, safe_name, atlas, save):
        root = Path(self.atlas_dir)
        target = root / safe_name
        temporary = root / f".{safe_name}.tmp-{os.getpid()}"
        if temporary.exists():
            shutil.rmtree(temporary)
        save(atlas, temporary)
        old = root / f".{safe_name}.old-{os.getpid()}"
        if target.exists():
            os.replace(target, old)
        try:
            os.replace(temporary, target)
        except BaseException:
            if old.exists():
                os.replace(old, target)
            raise
        if old.exists():
            shutil.rmtree(old)

    def atlas_paths(self):
        root = Path(self.atlas_dir)
        return sorted(path for path in root.iterdir() if path.is_dir() and
                      not path.name.startswith("."))

    def delete_atlases(self, safe_names):
        root = Path(self.atlas_dir).resolve()
        for safe_name in safe_names:
            target = (root / safe_name).resolve()
            if target.parent == root and target.is_dir():
                shutil.rmtree(target)

    # ------------------------------------------------------------------
    #  Calibration thresholds
    # ------------------------------------------------------------------

    def load_thresholds(self):
        """Return threshold dict or None if the file is missing/corrupt."""
        try:
            with open(self._threshold_file, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning("Failed to load calibration thresholds: %s", e)
            return None

    def save_thresholds(self, data):
        """Persist threshold data atomically (write-then-rename would be ideal)."""
        os.makedirs(self.calibration_dir, mode=0o700, exist_ok=True)
        tmp = self._threshold_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._threshold_file)

    # ------------------------------------------------------------------
    #  Calibration report
    # ------------------------------------------------------------------

    def save_report(self, data):
        os.makedirs(self.calibration_dir, mode=0o700, exist_ok=True)
        with open(self._report_file, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.chmod(self._report_file, 0o600)

    # ------------------------------------------------------------------
    #  Calibration samples
    # ------------------------------------------------------------------

    def save_sample(self, base_name, npz_data, meta):
        """Save a raw calibration sample (.npz + .json sidecar)."""
        json_path = os.path.join(self.sample_dir, base_name + ".json")
        npz_path = os.path.join(self.sample_dir, base_name + ".npz")

        with open(json_path, "w") as f:
            json.dump(meta, f)
        os.chmod(json_path, 0o600)

        np.savez(npz_path, **npz_data)
        os.chmod(npz_path, 0o600)

    def list_samples(self):
        """Yield (base_name, meta, frames) for each sample in sample_dir."""
        if not os.path.isdir(self.sample_dir):
            return
        for filename in sorted(os.listdir(self.sample_dir)):
            if not filename.endswith(".npz"):
                continue
            base = filename[:-4]
            json_path = os.path.join(self.sample_dir, base + ".json")
            npz_path = os.path.join(self.sample_dir, filename)

            try:
                with open(json_path, "r") as f:
                    meta = json.load(f)
            except Exception as e:
                logger.warning("Skipping sample %s: %s", base, e)
                continue

            try:
                npz = np.load(npz_path, allow_pickle=False)
            except Exception as e:
                logger.warning("Skipping sample %s: %s", base, e)
                continue

            num_frames = meta.get("num_frames", 0)
            frames = []
            for i in range(num_frames):
                key = f"frame_{i}"
                if key in npz:
                    frames.append(npz[key].tobytes())
                else:
                    break

            yield base, meta, frames
