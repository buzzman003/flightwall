"""Actual rotation position, including individual pages and privacy changes."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

from PIL import Image, ImageDraw

from flightwall.display import RotationCursor
from flightwall.household_policy import build_rotation
from flightwall.render import _deck_progress, CYAN, LINE, BG

NOW = datetime(2026, 10, 31, 18, tzinfo=timezone.utc)


def squares(count, index):
    frame = Image.new('RGB', (512, 256), BG)
    _deck_progress(ImageDraw.Draw(frame), {'deck_count':count,'deck_index':index})
    return frame


def test_seven_cards_show_seven_squares_current_advances_left_to_right_and_wraps():
    entries = [{'key':str(i),'view':'home','duration_seconds':15} for i in range(7)]
    cursor = RotationCursor(entries,0)
    for i in range(8):
        cursor.tick(entries,i*15)
        assert cursor.index == i % 7
        frame=squares(7,cursor.index)
        left=497-7*8
        for slot in range(7):
            assert frame.getpixel((left+slot*8+2,244)) == (CYAN if slot==cursor.index else LINE)


def test_large_deck_is_bounded_and_reports_true_index_total_without_view_types():
    with patch('flightwall.render._text') as text:
        frame=squares(65,31)
    assert any(call.args[2]=='32/65' for call in text.call_args_list)
    assert frame.getpixel((497-12*8+7*8+2,239))==CYAN
    for count,index in ((False,0),(0,0),(7,-1),(7,7),(257,0),('7',0)):
        assert squares(count,index).tobytes() == squares(0,0).tobytes()


def test_private_cards_never_contribute_to_the_eligible_deck():
    config={'demo':False,'timezone':'America/Chicago','lisa_private_enabled':True,
            'messages_enabled':True,'inspiration_enabled':True,'presence_enabled':True}
    status={'snapshot':{'demo':False,'flights':[]},'messages':{'lisa':[{'id':'private',
             'owner':'lisa','category':'personal','title':'hidden'}]},'presence':{},'kids':{}}
    baseline=build_rotation(status,config,NOW)
    with_private_candidates=deepcopy(status)
    with_private_candidates['inspiration']=[{'id':'art','owner':'lisa'}]
    assert len(build_rotation(with_private_candidates,config,NOW))==len(baseline)
    cursor=RotationCursor(baseline,0)
    # Retaining a cursor never retains the count of a removed private slot.
    cursor.reconcile(baseline+[{'key':'temporarily-visible','view':'inspiration','duration_seconds':15}],0)
    cursor.select(baseline+[{'key':'temporarily-visible','view':'inspiration','duration_seconds':15}],len(baseline),0)
    cursor.reconcile(baseline,1)
    assert cursor.index < len(baseline)
