"""Operating status joins the private TV rotation without paid I/O."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
from unittest.mock import patch

from flightwall.backend import Runtime
from flightwall.config import DEFAULTS
from flightwall.display import entry_render_state, fresh_household_status
from flightwall.household_policy import build_rotation
from flightwall.render import render_frame

NOW=datetime(2026,10,31,18,tzinfo=timezone.utc)
FLIGHT={'id':'fixture','number':'DL123','origin':'ATL','destination':'MEM',
        'start':NOW.isoformat(),'end':(NOW+timedelta(hours=2)).isoformat(),'deadhead':False}


def test_runtime_and_rotation_keep_provider_state_separate_and_age_it_each_frame():
    with tempfile.TemporaryDirectory() as folder:
        runtime=Runtime(Path(folder)/'data',Path(folder)/'install')
        runtime.config.update({'demo':False,'flightaware_enabled':True,'airline':'DL','timezone':'America/Chicago'})
        (runtime.data_dir/'flightaware.key').write_text('synthetic-key')
        runtime.snapshot=lambda: {'demo':False,'flights':[deepcopy(FLIGHT)],'last_sync':NOW.isoformat()}
        provider={'status':'fresh','source':'FlightAware AeroAPI','fetched_at':NOW.isoformat(),
                  'flight_id':'fixture','phase':'airborne','number':'DL123','origin':'ATL','destination':'MEM',
                  'arrival':{'comparison':'late','delta_minutes':12}}
        runtime.feeds._set_operating_status(runtime.config.get(),FLIGHT,provider)
        with patch('flightwall.feeds.request_json') as network:
            status=runtime.display_status(NOW)
            network.assert_not_called()
        config=runtime.config.public()
        entry=next(e for e in build_rotation(status,config,NOW) if e['view']=='flight_status')
        state=entry_render_state(status,config,entry,NOW)
        assert state['operating_status']['status']=='fresh'
        stale=fresh_household_status(status,config,NOW+timedelta(seconds=301))
        assert stale['operating_status']['status']=='stale'
        offline=fresh_household_status(status,config,NOW,backend_available=False)
        assert offline['operating_status']['status']=='stale'
        labels=[]
        from flightwall import render
        original=render._text
        def record(draw,xy,value,*args,**kwargs):
            labels.append(str(value));return original(draw,xy,value,*args,**kwargs)
        with patch.object(render,'_text',side_effect=record):
            render_frame(entry_render_state(offline,config,entry,NOW),config)
        assert 'PROVIDER UPDATE STALE' in labels
        assert not any('12 MIN LATE' in label for label in labels)
        assert status['operating_status']['status']=='fresh'


def test_disabled_or_outside_window_never_adds_a_provider_card():
    status={'snapshot':{'demo':False,'flights':[FLIGHT]},'operating_flight':FLIGHT,
            'operating_status':{'status':'fresh','flight_id':'fixture','phase':'airborne','fetched_at':NOW.isoformat()}}
    for config,now in ((DEFAULTS|{'demo':False},NOW),
                       (DEFAULTS|{'demo':False,'flightaware_enabled':True},NOW+timedelta(days=1))):
        assert not any(e['view']=='flight_status' for e in build_rotation(status,config,now))
