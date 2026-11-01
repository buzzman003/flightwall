"""Original transition variants use fictional pixels, deterministic and bounded."""
import hashlib

from PIL import Image
import pytest

from flightwall.transitions import EFFECTS, compose


@pytest.fixture
def boards():
    old = Image.new("RGB", (512, 256))
    old.putdata([(x % 256, y, 19) for y in range(256) for x in range(512)])
    current = Image.new("RGB", (512, 256))
    current.putdata([(91, (x // 2) % 256, 255 - y) for y in range(256) for x in range(512)])
    return old, current


def test_twenty_five_genuinely_distinct_midpoint_variants(boards):
    old, current = boards
    assert len(EFFECTS) == len(set(EFFECTS)) == 25
    results = [compose(old, current, .5, effect, seed=7) for effect in EFFECTS]
    assert len({hashlib.sha256(image.tobytes()).digest() for image in results}) == 25
    assert all(image.mode == "RGB" and image.size == (512, 256) for image in results)


@pytest.mark.parametrize("effect", EFFECTS)
def test_exact_endpoints_and_source_immutability(boards, effect):
    old, current = boards
    old.info["geographic_patch"] = "old private metadata is not a presentation layer"
    current.info["fictional"] = True
    snapshots = old.tobytes(), current.tobytes(), dict(old.info), dict(current.info)
    for progress, expected in ((-1, old), (0, old), (1, current), (2, current)):
        actual = compose(old, current, progress, effect)
        assert actual.tobytes() == expected.tobytes()
        assert actual is not expected and actual.info == {}
    assert compose(old, current, .37, effect, seed=3).tobytes() == compose(old, current, .37, effect, seed=3).tobytes()
    assert (old.tobytes(), current.tobytes(), old.info, current.info) == snapshots


@pytest.mark.parametrize("effect", EFFECTS)
def test_no_flash_frame_or_interpolated_colors(effect):
    old = Image.new("RGB", (97, 49), (177, 13, 23))
    current = Image.new("RGB", old.size, (27, 101, 211))
    for progress in (.01, .24, .5, .76, .99):
        image = compose(old, current, progress, effect, seed=11)
        assert set(image.getdata()) <= {(177, 13, 23), (27, 101, 211)}


def test_opposite_wipes_and_slides_move_in_their_stated_direction(boards):
    old, current = boards
    for toward, entry in (("left", (511, 128)), ("right", (0, 128)),
                          ("up", (256, 255)), ("down", (256, 0))):
        assert compose(old, current, .5, "wipe_" + toward).getpixel(entry) == current.getpixel(entry)
    left = compose(old, current, .25, "slide_left")
    assert left.getpixel((0, 10)) == old.getpixel((128, 10))
    assert left.getpixel((384, 10)) == current.getpixel((0, 10))
    right = compose(old, current, .25, "slide_right")
    assert right.getpixel((128, 10)) == old.getpixel((0, 10))
    assert right.getpixel((0, 10)) == current.getpixel((384, 10))
    up = compose(old, current, .25, "slide_up")
    assert up.getpixel((10, 0)) == old.getpixel((10, 64))
    assert up.getpixel((10, 192)) == current.getpixel((10, 0))
    down = compose(old, current, .25, "slide_down")
    assert down.getpixel((10, 64)) == old.getpixel((10, 0))
    assert down.getpixel((10, 0)) == current.getpixel((10, 192))


def test_random_variants_use_only_the_supplied_seed_without_global_random_state(boards):
    import random
    old, current = boards
    before = random.getstate()
    for effect in ("dissolve_fine", "dissolve_blocks", "tiles_diagonal"):
        assert compose(old, current, .47, effect, seed=2).tobytes() != compose(old, current, .47, effect, seed=3).tobytes()
    assert random.getstate() == before


@pytest.mark.parametrize("progress", (None, True, "0.5", float("nan"), float("inf"), float("-inf")))
def test_bad_progress_never_retains_old_pixels(boards, progress):
    old, current = boards
    assert compose(old, current, progress, "iris").tobytes() == current.tobytes()


def test_bad_old_frame_or_unknown_effect_fail_closed_to_current(boards):
    old, current = boards
    for previous in (None, Image.new("RGB", (1, 1)), old.convert("RGBA")):
        assert compose(previous, current, .2, "iris").tobytes() == current.tobytes()
    assert compose(old, current, 0, "unknown").tobytes() == current.tobytes()
    for invalid in (None, old.convert("L"), Image.new("RGB", (513, 256)), Image.new("RGB", (512, 257))):
        with pytest.raises(ValueError):
            compose(old, invalid, .5, "iris")
