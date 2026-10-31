"""Release gate helpers fail closed without executing a native application."""
import importlib.util
import io
import json
import marshal
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "validate-native.py"
spec = importlib.util.spec_from_file_location("native_validation", MODULE)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class NativeGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def archive(self, entries):
        path = self.root / "fixture.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, value, kind in entries:
                item = tarfile.TarInfo(name)
                item.type = kind
                if kind == tarfile.REGTYPE:
                    item.size = len(value)
                    archive.addfile(item, io.BytesIO(value))
                else:
                    item.linkname = value
                    archive.addfile(item)
        return path

    def test_original_baseline_is_pinned_and_modified_archive_is_rejected(self):
        path = self.root / "not-the-original.tar.gz"
        path.write_bytes(b"synthetic different installer")
        self.assertEqual(gate.BASELINE_SHA256, "086bf6267e3c96b55de1cfda70ec3adff21c65aff19bb5a525936a70b969de08")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            gate.verify_baseline(path)

    def test_glibc_audit_uses_required_symbols_and_rejects_private_abis(self):
        symbols = "\n".join(("000 *UND* (GLIBC_2.36) arc4random", "000 *UND* GLIBC_2.2.5 malloc",
                              "000 .text GLIBC_2.39 provided_symbol"))
        self.assertEqual(gate.required_glibc(symbols), {(2, 36), (2, 2, 5)})
        with self.assertRaisesRegex(ValueError, "ABI"):
            gate.required_glibc("000 *UND* (GLIBC_PRIVATE) private_symbol")
        with self.assertRaisesRegex(ValueError, "ABI"):
            gate.required_glibc("000 *UND* (GLIBC_ABI_DT_RELR) placeholder")

    def test_code_comparison_normalizes_build_paths_but_detects_real_edits(self):
        first = compile("def f():\n    return 1\n", "/build/a.py", "exec")
        same = compile("def f():\n    return 1\n", "/other/a.py", "exec")
        changed = compile("def f():\n    return 2\n", "/build/a.py", "exec")
        self.assertEqual(gate.normalized_code(first), gate.normalized_code(same))
        self.assertNotEqual(gate.normalized_code(first), gate.normalized_code(changed))

    def test_frozen_audit_allows_only_absent_alternative_source_entrypoint(self):
        source, release = self.root / "source", self.root / "release"
        (source / "flightwall").mkdir(parents=True)
        (source / "assets").mkdir()
        (release / "_internal" / "assets").mkdir(parents=True)
        (source / "assets" / "fixture.txt").write_bytes(b"synthetic asset")
        (release / "_internal" / "assets" / "fixture.txt").write_bytes(b"synthetic asset")
        files = {"flightwall_entry.py": "from flightwall.main import main\n",
                 "flightwall/__init__.py": "__version__ = '0.4.0'\n",
                 "flightwall/__main__.py": "from .main import main\nmain()\n",
                 "flightwall/main.py": "def main():\n    return 0\n"}
        for name, content in files.items():
            (source / name).write_text(content)
        compiled = {"flightwall": compile(files["flightwall/__init__.py"], "__init__.py", "exec"),
                    "flightwall.main": compile(files["flightwall/main.py"], "main.py", "exec")}
        entry = marshal.dumps(compile(files["flightwall_entry.py"], "entry.py", "exec"))
        archive = SimpleNamespace(toc={"PYZ.pyz": ("z",), "flightwall_entry": ("s",)},
                                  extract=lambda name: entry,
                                  open_embedded_archive=lambda name: SimpleNamespace(
                                      toc=compiled, extract=lambda module: compiled[module]))
        with patch("PyInstaller.archive.readers.CArchiveReader", return_value=archive):
            report = gate.audit_frozen_source(release, source)
            self.assertEqual(report["source_only_entrypoints"], ["flightwall.__main__"])
            self.assertEqual(report["application_modules"], ["flightwall", "flightwall.main"])
            compiled.pop("flightwall.main")
            with self.assertRaisesRegex(ValueError, "missing an application module: flightwall.main"):
                gate.audit_frozen_source(release, source)
            compiled["flightwall.main"] = compile(files["flightwall/main.py"], "main.py", "exec")
            compiled["flightwall.__main__"] = compile("raise RuntimeError('changed')", "__main__.py", "exec")
            with self.assertRaisesRegex(ValueError, "differs from the tested source: flightwall.__main__"):
                gate.audit_frozen_source(release, source)
            archive.toc.pop("flightwall_entry")
            with self.assertRaisesRegex(ValueError, "missing its application entry point"):
                gate.audit_frozen_source(release, source)

    def test_installer_rejects_traversal_duplicates_special_files_and_external_links(self):
        prefix = "flightwall-batocera-0.4.0/"
        fixtures = [[("../outside", b"bad", tarfile.REGTYPE)],
                    [(prefix + "VERSION", b"0.4.0", tarfile.REGTYPE)] * 2,
                    [(prefix + "pipe", "", tarfile.FIFOTYPE)],
                    [(prefix + "external", "/outside", tarfile.SYMTYPE)],
                    [(prefix + "external", "../../outside", tarfile.SYMTYPE)]]
        for index, entries in enumerate(fixtures):
            with self.subTest(index=index), self.assertRaises((ValueError, tarfile.TarError)):
                gate.extract_installer(self.archive(entries), self.root / str(index), "0.4.0")
        self.assertFalse((self.root / "outside").exists())

    def test_regular_installer_and_exact_report_inventory(self):
        prefix = "flightwall-batocera-0.4.0/"
        package = gate.extract_installer(self.archive([
            (prefix + "VERSION", b"0.4.0", tarfile.REGTYPE),
            (prefix + "releases/0.4.0/VERSION", b"0.4.0", tarfile.REGTYPE),
            (prefix + "releases/0.4.0/FlightWall", b"synthetic native fixture", tarfile.REGTYPE)]), self.root / "output", "0.4.0")
        release = package / "releases" / "0.4.0"
        report = self.root / "report.json"
        value = {"passed": True, "version": "0.4.0", "baseline_sha256": gate.BASELINE_SHA256,
                 "bundle_files": gate.inventory(release)}
        report.write_text(json.dumps(value))
        self.assertEqual(gate.verify_report(release, report), value)
        (release / "FlightWall").write_bytes(b"changed after validation")
        with self.assertRaisesRegex(ValueError, "changed"):
            gate.verify_report(release, report)

    def test_report_rejects_missing_success_or_wrong_baseline(self):
        release = self.root / "release"
        release.mkdir()
        (release / "VERSION").write_text("0.4.0")
        report = self.root / "report.json"
        for value in ({"passed": False}, {"passed": True, "baseline_sha256": "0" * 64}):
            report.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "successful"):
                gate.verify_report(release, report)

    def test_launcher_output_requires_actual_gui_result_among_helper_messages(self):
        log = 'helper text\n{"status":"activated"}\n{"frames":4,"shader":true}\n'
        self.assertEqual(gate.json_objects(log), [{"status": "activated"}, {"frames": 4, "shader": True}])
        self.assertEqual(gate.json_objects('not JSON { broken\n'), [])


if __name__ == "__main__":
    unittest.main()
