"""The shipped preview is a real silent 15-second H.264 video, not live data."""
import importlib.util
import json
from pathlib import Path
import struct

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets/ports"


def atoms(data):
    offset = 0
    while offset < len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        assert size >= 8 and offset + size <= len(data)
        yield kind, data[offset + 8:offset + size]
        offset += size


def child(data, name):
    return next(payload for kind, payload in atoms(data) if kind == name)


def test_encoded_preview_has_exact_duration_frames_dimensions_h264_and_no_audio():
    data = (ASSETS / "flightwall-preview.mp4").read_bytes()
    top = list(atoms(data))
    assert next(i for i, row in enumerate(top) if row[0] == b"moov") < next(i for i, row in enumerate(top) if row[0] == b"mdat")
    moov = child(data, b"moov")
    tracks = [payload for kind, payload in atoms(moov) if kind == b"trak"]
    assert len(tracks) == 1
    mdia = child(tracks[0], b"mdia")
    assert child(mdia, b"hdlr")[8:12] == b"vide"
    mdhd = child(mdia, b"mdhd")
    assert mdhd[0] == 0
    scale, duration = struct.unpack_from(">II", mdhd, 12)
    assert duration / scale == 15
    stbl = child(child(mdia, b"minf"), b"stbl")
    stsd = child(stbl, b"stsd")
    assert struct.unpack_from(">I", stsd, 4)[0] == 1
    codec, visual = next(atoms(stsd[8:]))
    assert codec == b"avc1" and struct.unpack_from(">HH", visual, 24) == (1024, 512)
    avcc = child(visual[78:], b"avcC")
    assert avcc[1] == 66 and avcc[3] == 31  # Baseline is 8-bit 4:2:0; level3.1.
    stts = child(stbl, b"stts")
    count = struct.unpack_from(">I", stts, 4)[0]
    assert sum(struct.unpack_from(">II", stts, 8 + index * 8)[0] for index in range(count)) == 450


def test_timeline_is_deterministic_and_uses_only_fictional_source_renderer():
    path = ROOT / "scripts/build-ports-preview.py"
    spec = importlib.util.spec_from_file_location("ports_preview", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    with Image.open(ASSETS / "flightwall-logo.png") as source:
        logo = source.convert("RGB")
    first = module.frame_at(420, logo).tobytes()
    module.frame_at(10, logo)
    assert module.frame_at(420, logo).tobytes() == first
    timeline = json.loads((ASSETS / "preview-timeline.json").read_text())
    assert timeline["frames"] == 450 and timeline["duration_seconds"] == 15 and timeline["audio_streams"] == 0
    assert timeline["style"]["glow"] == 0
    assert {entry["view"] for entry in timeline["timeline"]} >= {"brand", "details", "trip_map", "schedule", "weather"}
    assert module.state_for("details", 90)["demo"] is True
    assert all(flight["number"].startswith("DEMO") for flight in module.state_for("details", 90)["flights"])
    assert not any(key in module.CONFIG for key in ("home_latitude", "home_longitude", "calendar_id"))
