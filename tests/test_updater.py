import base64
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import warnings
from unittest.mock import patch
from urllib.request import Request
import zipfile
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flightwall import updater


def _symlink(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            raise unittest.SkipTest("Windows symlink privilege unavailable; Linux must run the native update/release checks") from None
        raise


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install = self.root / "install"
        self.data = self.root / "data"
        old = self.install / "releases" / "0.1.0"
        old.mkdir(parents=True)
        (old / "FlightWall").write_bytes(b"old executable")
        (old / "VERSION").write_text("0.1.0\n", encoding="ascii")
        _symlink(self.install / "current", "releases/0.1.0", directory=True)
        self.data.mkdir()
        (self.data / "token.json").write_text("private calendar credential")
        self.key = Ed25519PrivateKey.generate()
        raw = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.config = {"updates_enabled": True, "update_manifest_url": "https://updates.example.test/manifest.json",
                       "update_public_key": base64.b64encode(raw).decode("ascii")}
        self.manager = updater.UpdateManager(self.install, self.data, self.config)

    def tearDown(self):
        self.temporary.cleanup()

    def archive(self, version="0.1.1", extra=None):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("FlightWall", b"new executable")
            archive.writestr("VERSION", version + "\n")
            archive.writestr("_internal/asset.txt", "bundled asset")
            for name, data in (extra or {}).items():
                archive.writestr(name, data)
        return output.getvalue()

    def manifest(self, archive, version="0.1.1"):
        value = {"version": version, "url": f"https://updates.example.test/{version}.zip",
                 "size": len(archive), "sha256": hashlib.sha256(archive).hexdigest()}
        value["signature"] = base64.b64encode(self.key.sign(updater.canonical_manifest(value))).decode("ascii")
        return value

    def fake_network(self, archive, manifest=None):
        manifest = self.manifest(archive) if manifest is None else manifest
        def fetch(url, target, limit, expected_size=None):
            value = json.dumps(manifest).encode() if url.endswith("manifest.json") else archive
            if len(value) > limit or (expected_size is not None and len(value) != expected_size):
                raise updater.UpdateError("Download size invalid.")
            target.write(value)
            return len(value)
        return patch.object(updater, "_download", side_effect=fetch)

    def assert_not_staged(self):
        self.assertFalse((self.install / "pending.json").exists())
        self.assertEqual((self.install / "current").resolve().name, "0.1.0")
        self.assertFalse((self.install / "releases" / "0.1.1").exists())

    def test_disabled_default_never_uses_network(self):
        manager = updater.UpdateManager(self.install, self.data, {})
        with patch.object(updater, "_download", side_effect=AssertionError("unexpected download")):
            self.assertEqual(manager.check()["status"], "disabled")
            self.assertEqual(manager.stage()["status"], "disabled")

    def test_stage_activate_and_confirm_preserves_credentials(self):
        archive = self.archive()
        with self.fake_network(archive):
            self.assertEqual(self.manager.check()["status"], "available")
            self.assertEqual(self.manager.stage()["status"], "staged")
            self.assertEqual(self.manager.check()["status"], "staged")
        self.assertEqual((self.install / "current").resolve().name, "0.1.0")
        self.assertEqual(updater.activate_pending(self.install, self.data)["status"], "activated")
        self.assertEqual((self.install / "current").resolve().name, "0.1.1")
        self.assertEqual(updater.mark_healthy(self.install, self.data, "0.1.1")["status"], "healthy")
        self.assertEqual(updater.activate_pending(self.install, self.data)["status"], "unchanged")
        self.assertEqual(updater.rollback(self.install, self.data)["status"], "unchanged")
        self.assertEqual((self.data / "token.json").read_text(), "private calendar credential")
        self.assertTrue((self.install / "releases" / "0.1.0" / "FlightWall").exists())
        self.assertTrue((self.install / "current" / "FlightWall").stat().st_mode & stat.S_IXUSR)

    def test_unhealthy_activation_rolls_back_on_next_launch(self):
        archive = self.archive()
        with self.fake_network(archive):
            self.assertEqual(self.manager.stage()["status"], "staged")
        updater.activate_pending(self.install, self.data)
        result = updater.activate_pending(self.install, self.data)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual((self.install / "current").resolve().name, "0.1.0")
        with self.fake_network(archive):
            self.assertEqual(self.manager.check()["status"], "error")
            self.assertIn("previously failed", self.manager.stage()["message"])

    def test_explicit_failed_start_rollback(self):
        archive = self.archive()
        with self.fake_network(archive):
            self.manager.stage()
        updater.activate_pending(self.install, self.data)
        self.assertEqual(updater.rollback(self.install, self.data)["status"], "rolled_back")
        self.assertEqual(updater.mark_healthy(self.install, self.data, "0.1.1")["status"], "error")

    def test_manifest_signature_tampering_is_rejected(self):
        archive = self.archive()
        manifest = self.manifest(archive)
        manifest["version"] = "0.1.2"
        with self.fake_network(archive, manifest):
            result = self.manager.stage()
        self.assertEqual(result["status"], "error")
        self.assertIn("signature", result["message"])
        self.assert_not_staged()

    def test_wrong_signing_key_is_rejected(self):
        archive = self.archive()
        raw = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.config["update_public_key"] = base64.b64encode(raw).decode("ascii")
        with self.fake_network(archive):
            self.assertEqual(self.manager.stage()["status"], "error")
        self.assert_not_staged()

    def test_archive_tampering_is_rejected(self):
        archive = self.archive()
        manifest = self.manifest(archive)
        altered = archive[:-1] + bytes([archive[-1] ^ 1])
        with self.fake_network(altered, manifest):
            result = self.manager.stage()
        self.assertEqual(result["status"], "error")
        self.assertIn("SHA-256", result["message"])
        self.assert_not_staged()

    def test_signed_manifest_cannot_introduce_commands(self):
        archive = self.archive()
        manifest = self.manifest(archive)
        manifest["command"] = "anything"
        manifest["signature"] = base64.b64encode(self.key.sign(updater.canonical_manifest(manifest))).decode("ascii")
        with self.fake_network(archive, manifest):
            self.assertIn("unexpected", self.manager.stage()["message"])
        self.assert_not_staged()

    def test_http_release_url_is_rejected_even_if_signed(self):
        archive = self.archive()
        manifest = self.manifest(archive)
        manifest["url"] = "http://updates.example.test/release.zip"
        manifest["signature"] = base64.b64encode(self.key.sign(updater.canonical_manifest(manifest))).decode("ascii")
        with self.fake_network(archive, manifest):
            self.assertIn("HTTPS", self.manager.stage()["message"])

    def test_downgrade_rejected_even_with_valid_signature(self):
        archive = self.archive("0.0.9")
        with self.fake_network(archive, self.manifest(archive, "0.0.9")):
            self.assertIn("downgrade", self.manager.stage()["message"])
        self.assert_not_staged()

    def test_rollback_does_not_lower_highest_accepted_version(self):
        archive = self.archive("0.2.0")
        with self.fake_network(archive, self.manifest(archive, "0.2.0")):
            self.manager.stage()
        updater.activate_pending(self.install, self.data)
        updater.rollback(self.install, self.data)
        lower = self.archive("0.1.1")
        with self.fake_network(lower):
            self.assertIn("downgrade", self.manager.stage()["message"])

    def test_traversal_paths_are_rejected(self):
        for name in ("../escaped", "/tmp/escaped", "_internal/../../escaped", "C:/escaped", "_internal\\escaped"):
            with self.subTest(name=name):
                archive = self.archive(extra={name: "bad"})
                with self.fake_network(archive):
                    self.assertEqual(self.manager.stage()["status"], "error")
                self.assert_not_staged()
                self.assertFalse((self.install / "releases" / "escaped").exists())

    def test_symlink_archive_is_rejected(self):
        output = io.BytesIO(self.archive())
        with zipfile.ZipFile(output, "a") as archive:
            entry = zipfile.ZipInfo("_internal/link")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "../../../../data/token.json")
        with self.fake_network(output.getvalue()):
            self.assertIn("link", self.manager.stage()["message"])
        self.assert_not_staged()

    def test_zip_bomb_is_rejected(self):
        archive = self.archive(extra={"_internal/bomb": b"0" * (3 * 1024 * 1024)})
        with self.fake_network(archive):
            self.assertIn("safe extraction", self.manager.stage()["message"])
        self.assert_not_staged()

    def test_mismatched_version_rejected(self):
        archive = self.archive("0.9.9")
        with self.fake_network(archive):
            self.assertEqual(self.manager.stage()["status"], "error")
        self.assert_not_staged()

    def test_duplicate_archive_file_rejected(self):
        output = io.BytesIO(self.archive())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(output, "a") as archive:
                archive.writestr("VERSION", "0.1.1\n")
        with self.fake_network(output.getvalue()):
            self.assertIn("duplicate", self.manager.stage()["message"])
        self.assert_not_staged()

    def test_public_key_can_be_provisioned_as_local_pem(self):
        (self.data / "updates-public.pem").write_bytes(self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        self.config["update_public_key"] = "updates-public.pem"
        with self.fake_network(self.archive()):
            self.assertEqual(self.manager.check()["status"], "available")

    def test_untrusted_network_errors_do_not_leak_details(self):
        with patch.object(updater, "_download", side_effect=OSError("secret token in URL")):
            result = self.manager.check()
        self.assertEqual(result["status"], "error")
        self.assertNotIn("secret", result["message"])

    def test_malformed_local_state_blocks_update(self):
        state_path = self.data / "updates" / "state.json"
        state_path.parent.mkdir()
        state_path.write_text('{"minimum_version": "../../bad"}')
        with self.fake_network(self.archive()):
            self.assertEqual(self.manager.stage()["status"], "error")
        self.assert_not_staged()

    def test_interrupted_stage_can_be_retried_without_overwriting_current(self):
        archive = self.archive()
        writer = updater._atomic_json
        def interrupted(path, value):
            if path == self.install / "pending.json":
                raise OSError("simulated power loss")
            writer(path, value)
        with self.fake_network(archive), patch.object(updater, "_atomic_json", side_effect=interrupted):
            self.assertEqual(self.manager.stage()["status"], "error")
        self.assertFalse((self.install / "pending.json").exists())
        self.assertEqual((self.install / "current").resolve().name, "0.1.0")
        with self.fake_network(archive):
            self.assertEqual(self.manager.stage()["status"], "staged")
        self.assertEqual(updater.activate_pending(self.install, self.data)["status"], "activated")

    def test_stale_partial_downloads_are_cleaned_without_touching_releases(self):
        releases = self.install / "releases"
        (releases / ".download-interrupted.zip").write_bytes(b"partial")
        (releases / ".staging-interrupted").mkdir()
        (releases / ".staging-interrupted" / "partial").write_bytes(b"partial")
        with self.fake_network(self.archive()):
            self.assertEqual(self.manager.stage()["status"], "staged")
        self.assertFalse((releases / ".download-interrupted.zip").exists())
        self.assertFalse((releases / ".staging-interrupted").exists())
        self.assertTrue((releases / "0.1.0" / "FlightWall").exists())


class ReleaseToolTests(unittest.TestCase):
    def test_http_redirect_and_embedded_credentials_rejected(self):
        # This pure validation does not need a filesystem lifecycle fixture.
        handler = updater.HTTPSOnlyRedirectHandler()
        with self.assertRaises(updater.UpdateError):
            handler.redirect_request(Request("https://example.test/file"), None, 302, "redirect", {}, "http://example.test/file")
        with self.assertRaises(updater.UpdateError):
            updater.validate_https_url("https://user:password@example.test/file")
        with self.assertRaises(updater.UpdateError):
            updater.validate_https_url("file:///tmp/release.zip")

    def test_build_signs_verified_archive_without_private_key(self):
        from scripts.make_release import build
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "frozen"
            source.mkdir()
            header = bytearray(64)
            header[:6] = b"\x7fELF\x02\x01"
            header[18:20] = (62).to_bytes(2, "little")
            (source / "FlightWall").write_bytes(header)
            (source / "VERSION").write_text("0.1.1\n")
            (source / "_internal").mkdir()
            (source / "_internal" / "library.so.1").write_bytes(b"library bytes")
            _symlink(source / "_internal" / "library.so", "library.so.1")
            private = Ed25519PrivateKey.generate()
            key = root / "private.pem"
            key.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
            with patch("builtins.print"):
                build(source, "0.1.1", "https://example.test/FlightWall-0.1.1-linux-x86_64.zip", key, root / "output")
            manifest = json.loads((root / "output" / "manifest.json").read_text())
            updater.verify_manifest(manifest, private.public_key())
            archive = root / "output" / "FlightWall-0.1.1-linux-x86_64.zip"
            with archive.open("rb") as handle:
                self.assertEqual(hashlib.file_digest(handle, "sha256").hexdigest(), manifest["sha256"])
            updater.safe_extract(archive, root / "extracted", "0.1.1")
            self.assertFalse((root / "extracted" / "private.pem").exists())
            self.assertFalse((root / "extracted" / "_internal" / "library.so").is_symlink())
            self.assertEqual((root / "extracted" / "_internal" / "library.so").read_bytes(), b"library bytes")

    def test_source_file_scan_rejects_credentials(self):
        from scripts.make_release import _source_files
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "token.json").write_text("private")
            with self.assertRaises(updater.UpdateError):
                _source_files(source, root / "signing.pem")

    def test_source_file_scan_rejects_external_links(self):
        from scripts.make_release import _source_files
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (root / "outside").write_text("private")
            _symlink(source / "outside-link", root / "outside")
            with self.assertRaises(updater.UpdateError):
                _source_files(source, root / "signing.pem")


if __name__ == "__main__":
    unittest.main()
