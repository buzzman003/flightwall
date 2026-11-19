"""Text from a bounded ticket PDF. The caller checks source and consent first."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


MAX_PDF_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 96 * 1024
MAX_PAGES = 6
_ASSETS = Path(__file__).resolve().parents[1] / "assets"


def extract_itinerary_text(data: bytes) -> str:
    """Return physical-layout UTF-8 text, or an empty string on any failure."""
    if (not isinstance(data, bytes) or len(data) > MAX_PDF_BYTES
            or not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]):
        return ""
    if sys.platform != "linux" or not hasattr(os, "memfd_create"):
        return ""
    try:
        # Anonymous memory files bound stdout without retaining ticket bytes
        # on disk. Only the empty mount point exists in the host filesystem.
        with os.fdopen(os.memfd_create("input", os.MFD_CLOEXEC), "w+b") as source, \
                os.fdopen(os.memfd_create("text", os.MFD_CLOEXEC), "w+b") as output, \
                tempfile.TemporaryDirectory(prefix="fw-pdf-") as mountpoint:
            source.write(data)
            source.seek(0)
            result = subprocess.run(
                ["/usr/bin/python3", "-I", "-B", "-S", str(_ASSETS / "helpers" / "ticket-pdf.py"),
                 mountpoint, str(MAX_PAGES), str(MAX_TEXT_BYTES)],
                stdin=source, stdout=output, stderr=subprocess.DEVNULL, timeout=8,
                cwd="/", env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}, close_fds=True,
            )
            if result.returncode or output.seek(0, os.SEEK_END) >= MAX_TEXT_BYTES:
                return ""
            output.seek(0)
            text = output.read(MAX_TEXT_BYTES).decode("utf-8", errors="strict")
            if not text.endswith("\f") or not 1 <= text.count("\f") <= MAX_PAGES:
                return ""
            return text
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return ""
