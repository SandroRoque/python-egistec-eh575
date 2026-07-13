import importlib.util
from importlib.machinery import SourceFileLoader
import json
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from egis_driver.version import __version__


ROOT = Path(__file__).resolve().parents[1]


def load_script(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script(ROOT / "tools" / "build-release", "build_release")
guard = load_script(ROOT / "tools" / "repository-guard", "repository_guard")


class ReleaseMetadataTests(unittest.TestCase):
    def test_compatibility_contracts_are_versioned_json(self):
        for name in (
            "doctor.schema.json",
            "lifecycle.schema.json",
            "report.schema.json",
            "known-good.json",
            "lifecycle.example.json",
        ):
            document = json.loads((ROOT / "compatibility" / name).read_text())
            if name.endswith(".schema.json"):
                self.assertIn("$schema", document)
            else:
                self.assertEqual(document["schema_version"], 1)

    def test_package_versions_match_canonical_version(self):
        arch = (ROOT / "packaging" / "arch" / "PKGBUILD.in").read_text()
        fedora = (
            ROOT / "packaging" / "fedora" / "open-fprintd-eh575.spec"
        ).read_text()

        self.assertEqual(re.search(r"^pkgver=(.+)$", arch, re.MULTILINE).group(1), __version__)
        self.assertEqual(
            re.search(r"^Version:\s+(.+)$", fedora, re.MULTILINE).group(1),
            __version__,
        )

    def test_deterministic_archive_and_rendered_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "open-fprintd-eh575" / "egis_driver").mkdir(parents=True)
            (repo / "packaging" / "arch").mkdir(parents=True)
            (repo / "packaging" / "fedora").mkdir(parents=True)
            (repo / release.VERSION_FILE).write_text('__version__ = "0.4.0"\n')
            (repo / release.ARCH_TEMPLATE).write_text(
                "pkgver=0.4.0\nsha256sums=('@SOURCE_SHA256@')\n"
            )
            (repo / release.FEDORA_SPEC).write_text("Version: 0.4.0\n")
            (repo / "open-fprintd-eh575" / "openfprintd").mkdir()
            (repo / "open-fprintd-eh575" / "openfprintd" / "__init__.py").write_text("")
            (repo / "README.md").write_text("fixture\n")
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

            old_root = release.ROOT
            release.ROOT = repo
            try:
                first = repo / "first"
                second = repo / "second"
                returned = release.build_release("HEAD", first)
                release.build_release("HEAD", second)
            finally:
                release.ROOT = old_root

            archive_name = "open-fprintd-eh575-0.4.0.tar.gz"
            self.assertEqual(
                (first / archive_name).read_bytes(),
                (second / archive_name).read_bytes(),
            )
            self.assertNotIn("@SOURCE_SHA256@", (first / "PKGBUILD").read_text())
            manifest = json.loads((first / "release-manifest.json").read_text())
            self.assertEqual(returned, manifest)
            self.assertEqual(manifest["version"], "0.4.0")
            with tarfile.open(first / archive_name, "r:gz") as archive:
                names = archive.getnames()
            self.assertTrue(all(
                name == "open-fprintd-eh575-0.4.0" or
                name.startswith("open-fprintd-eh575-0.4.0/")
                for name in names
            ))

    def test_package_recipes_install_the_same_runtime_contract(self):
        arch = (ROOT / release.ARCH_TEMPLATE).read_text()
        fedora = (ROOT / release.FEDORA_SPEC).read_text()
        for source in (
            "open-fprintd.service",
            "egis-bridge.service",
            "net.reactivated.fprint.policy",
            "io.github.uunicorn.Fprint.Device.Egis.conf",
            "70-egis-eh575.rules",
            "egis-doctor",
        ):
            self.assertIn(source, arch)
            self.assertIn(source, fedora)


class RepositoryGuardTests(unittest.TestCase):
    def test_private_and_generated_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "safe.txt").write_text("safe\n")
            (root / "private.txt").write_text("/" + "home/alice/fingerprint\n")
            (root / "capture.npz").write_bytes(b"data")

            errors = guard.inspect_files(
                [Path("safe.txt"), Path("private.txt"), Path("capture.npz")],
                root,
            )

        self.assertEqual(len(errors), 2)
        self.assertTrue(any("absolute home path" in error for error in errors))
        self.assertTrue(any("forbidden capture" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
