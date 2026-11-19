"""Synthetic ticket text exercises the same system reader used on Batocera."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flightwall import itinerary_pdf


def synthetic_pdf(pages):
    """Generate selectable text with two independently positioned columns."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    children = []
    for label in pages:
        page_number = len(objects) + 1
        children.append(f"{page_number} 0 R")
        page = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_number + 1} 0 R >>")
        content = f"BT /F1 12 Tf 35 720 Td ({label}) Tj 200 0 Td (AAA - BBB) Tj ET".encode()
        objects.extend([page.encode(), f"<< /Length {len(content)} >>\nstream\n".encode()
                        + content + b"\nendstream"])
    objects[1] = f"<< /Type /Pages /Count {len(pages)} /Kids [{' '.join(children)}] >>".encode()
    pdf = bytearray(b"%PDF-1.4\n")
    positions = [0]
    for index, obj in enumerate(objects, 1):
        positions.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(pdf)
    pdf.extend(f"xref\n0 {len(positions)}\n0000000000 65535 f \n".encode())
    for position in positions[1:]:
        pdf.extend(f"{position:010} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(positions)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(pdf)


class TicketPDFTests(unittest.TestCase):
    def test_rejects_oversize_non_pdf_truncated_or_wrong_type_before_starting(self):
        pdf = synthetic_pdf(["AA 123"])
        with patch.object(itinerary_pdf.subprocess, "run") as run:
            for invalid in (pdf[:-8], b"<html>", b"%PDF-" + b"x" * itinerary_pdf.MAX_PDF_BYTES,
                            None, bytearray(pdf)):
                self.assertEqual(itinerary_pdf.extract_itinerary_text(invalid), "")
            run.assert_not_called()

    def test_native_reader_extracts_layout_and_caps_pages(self):
        pdf = synthetic_pdf([f"AA {number}" for number in range(101, 107)])
        text = itinerary_pdf.extract_itinerary_text(pdf)
        if sys.platform != "linux":
            self.assertEqual(text, "")
            return
        self.assertIn("AA 101", text)
        self.assertRegex(text, r"AA 101  +AAA - BBB")
        self.assertIn("AA 106", text)
        self.assertEqual(itinerary_pdf.extract_itinerary_text(synthetic_pdf(["AA 107"] * 7)), "")
        self.assertEqual(itinerary_pdf.extract_itinerary_text(b"%PDF-1.4\ninvalid\n%%EOF"), "")

    def test_linux_worker_cannot_read_an_unrelated_file_or_open_a_socket(self):
        if sys.platform != "linux":
            return
        check = """
import runpy, socket, sys
worker = runpy.run_path(sys.argv[1])
worker['isolate'](sys.argv[2], 98304)
try:
    with open(sys.argv[3]) as f:
        f.read(1)
except OSError:
    pass
else:
    raise SystemExit(1)
try:
    socket.socket()
except PermissionError:
    pass
else:
    raise SystemExit(1)
"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            root.chmod(0o755)
            private = root / "synthetic-letter"
            private.write_text("fictional")
            private.chmod(0o644)
            mount = root / "mount"
            mount.mkdir()
            subprocess.run(["/usr/bin/python3", "-I", "-B", "-S", "-c", check,
                            str(itinerary_pdf._ASSETS / "helpers" / "ticket-pdf.py"),
                            str(mount), str(private)], check=True, timeout=8)

    def test_failures_and_full_output_are_discarded(self):
        pdf = synthetic_pdf(["AA 123"])
        if sys.platform != "linux":
            self.assertEqual(itinerary_pdf.extract_itinerary_text(pdf), "")
            return

        def fill_output(argv, **kwargs):
            self.assertEqual(kwargs["stdin"].read(), pdf)
            self.assertNotIn("LD_LIBRARY_PATH", kwargs["env"])
            kwargs["stdout"].write(b"X" * itinerary_pdf.MAX_TEXT_BYTES)
            self.assertTrue(Path(argv[-3]).is_dir())
            return subprocess.CompletedProcess(argv, 0)

        for failure in (FileNotFoundError(), subprocess.TimeoutExpired("parser", 8), fill_output):
            with patch.object(itinerary_pdf.subprocess, "run", side_effect=failure):
                self.assertEqual(itinerary_pdf.extract_itinerary_text(pdf), "")
