from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
import os
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from PIL import Image
import pytest

from flightwall import radar
from flightwall.flight_environment import (
    FAA_URL, RADAR_URL, MAX_RESPONSE, FlightEnvironmentFeed, _parse_faa,
    _radar_context, _radar_png, request_bytes,
)
from flightwall.trip_weather import select_airports

UTC = timezone.utc
NOW = datetime(2026, 11, 1, 1, 30, tzinfo=UTC)


def snapshot(now=NOW):
    return {"demo": False, "flights": [{
        "origin": "ATL", "destination": "JFK", "deadhead": False,
        "start": (now + timedelta(hours=2)).isoformat(),
        "end": (now + timedelta(hours=4)).isoformat(),
        "id": "PRIVATE_CALENDAR_ID", "description": "PRIVATE_DESCRIPTION",
    }]}


def image_bytes(size=(600, 392), format="GIF"):
    stream = BytesIO()
    Image.new("RGB", size, "navy").save(stream, format=format)
    return stream.getvalue()


def faa_bytes(clock="Sun Nov 1 01:30:00 2026 GMT"):
    return ("""<AIRPORT_STATUS_INFORMATION><Update_Time>""" + clock + """</Update_Time>
      <Dtd_File>https://untrusted.invalid/do-not-follow</Dtd_File>
      <Delay_type><Name>Ground Stops</Name><Ground_Stop_List>
        <Program><ARPT>ATL</ARPT><Reason>WEATHER / THUNDERSTORMS</Reason><End_Time>02:15 UTC</End_Time></Program>
        <Program><ARPT>LAX</ARPT><Reason>LOW CEILINGS</Reason></Program>
      </Ground_Stop_List></Delay_type>
      <Delay_type><Ground_Delay_List><Ground_Delay><ARPT>JFK</ARPT><Reason>WIND</Reason>
        <Avg>30 minutes</Avg><Max>1 hour</Max></Ground_Delay></Ground_Delay_List></Delay_type>
      <Delay_type><Arrival_Departure_Delay_List><Delay><ARPT>ATL</ARPT><Reason>VOLUME</Reason>
        <Arrival_Departure Type="Arrival"><Min>15 minutes</Min><Max>45 minutes</Max></Arrival_Departure>
        <Arrival_Departure Type="Departure"><Min>16 minutes</Min><Max>30 minutes</Max></Arrival_Departure>
        <Arrival_Departure Type="Unknown"><Min>900 minutes</Min></Arrival_Departure>
      </Delay></Arrival_Departure_Delay_List></Delay_type>
      <Delay_type><Airport_Closure_List><Airport><ARPT>ATL</ARPT><Reason>GA ONLY PRIVATE_CONTACT</Reason></Airport></Airport_Closure_List></Delay_type>
      <Delay_type><Unknown_Program><ARPT>ATL</ARPT><Reason>UNKNOWN_CLOSURE</Reason></Unknown_Program></Delay_type>
    </AIRPORT_STATUS_INFORMATION>""").encode()


@pytest.fixture
def setup_feed(tmp_path):
    config = {"demo": False, "national_radar_enabled": True, "airport_alerts_enabled": True,
              "timezone": "America/Chicago", "show_deadheads": False}
    feed = FlightEnvironmentFeed(tmp_path, lambda: dict(config))
    return feed, config


def poll(feed, now=NOW, **kwargs):
    def response(url):
        if url == FAA_URL:
            return kwargs.get("faa", faa_bytes()), None
        if url == radar.CAPABILITIES_URL:
            observed = kwargs.get("observed_at", now.isoformat())
            return ('''<WMS_Capabilities xmlns="http://www.opengis.net/wms"><Capability><Layer><Layer>
                <Name>conus_bref_qcd</Name><Dimension name="time" units="ISO8601">'''
                + str(observed) + '</Dimension></Layer></Layer></Capability></WMS_Capabilities>').encode(), None
        if url == radar.LEGEND_URL:
            return image_bytes((500, 30), format="PNG"), None
        if url in [radar.map_url(scope, kwargs.get("observed_at", now.isoformat())) for scope in radar.SCOPES]:
            return kwargs.get("radar", image_bytes(radar.SIZES["national"], format="PNG")), None
        raise AssertionError("Unexpected fixture request")
    with patch("flightwall.flight_environment.request_bytes", side_effect=response) as network:
        state = feed.poll(kwargs.get("snapshot", snapshot()), now)
    return state, network


def test_ready_sources_keep_distinct_clocks_and_filter_only_selected_airports(setup_feed):
    feed, _ = setup_feed
    state, network = poll(feed, observed_at=(NOW - timedelta(minutes=3)).isoformat())
    assert network.call_count == 4
    assert state["radar"]["status"] == state["airport_alerts"]["status"] == "ready"
    assert state["radar"]["issued_at"] != state["radar"]["fetched_at"]
    assert state["radar"]["source_time"] == state["radar"]["issued_at"]
    assert {row["airport"] for row in state["airport_alerts"]["events"]} == {"ATL", "JFK"}
    assert {row["kind"] for row in state["airport_alerts"]["events"]} == {
        "ground_stop", "ground_delay", "arrival_delay", "departure_delay"}
    radar_file = feed.image_path.read_bytes()
    assert hashlib.sha256(radar_file).hexdigest() == state["radar"]["image_sha256"]
    with Image.open(BytesIO(radar_file)) as image:
        assert image.format == "PNG" and image.mode == "RGB"
        assert image.size == (radar.SIZES["national"][0], radar.SIZES["national"][1] + radar.FOOTER_HEIGHT)
    serialized = json.dumps(state)
    disk = feed.path.read_text()
    for private in ("PRIVATE_CALENDAR_ID", "PRIVATE_DESCRIPTION", "PRIVATE_CONTACT", "UNKNOWN_CLOSURE", str(feed.path), str(feed.image_path)):
        assert private not in serialized and private not in disk
    assert "flights" not in disk and "Dtd_File" not in disk
    if os.name != "nt":
        assert feed.path.stat().st_mode & 0o777 == 0o600
        assert feed.image_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("setting,value,section,status", [
    ("demo", True, "radar", "demo"),
    ("demo", True, "airport_alerts", "demo"),
    ("national_radar_enabled", False, "radar", "off"),
    ("airport_alerts_enabled", False, "airport_alerts", "off"),
])
def test_disabled_or_demo_never_requests_that_feed(setup_feed, setting, value, section, status):
    feed, config = setup_feed
    config[setting] = value
    with patch("flightwall.flight_environment.request_bytes", side_effect=OSError) as network:
        state = feed.poll(snapshot(), NOW)
    assert state[section]["status"] == status
    requested = [call.args[0] for call in network.call_args_list]
    assert (radar.CAPABILITIES_URL if section == "radar" else FAA_URL) not in requested


@pytest.mark.parametrize("data", [{"demo": True}, {"demo": False, "flights": []},
    {"demo": False, "flights": [{**snapshot()["flights"][0], "deadhead": True}]}])
def test_no_schedule_context_never_requests(setup_feed, data):
    feed, _ = setup_feed
    with patch("flightwall.flight_environment.request_bytes") as network:
        state = feed.poll(data, NOW)
    network.assert_not_called()
    assert state["radar"]["status"] in {"outside_window", "demo"}
    assert not state["radar"]["image_available"] and not state["airport_alerts"]["events"]


def test_context_end_and_guest_independent_consent_are_rechecked_without_network(setup_feed):
    feed, config = setup_feed
    poll(feed)
    with patch("flightwall.flight_environment.request_bytes") as network:
        state = feed.status(NOW + timedelta(days=2))
    network.assert_not_called()
    assert state["radar"]["status"] == "outside_window"
    assert not state["radar"]["image_available"] and not state["airport_alerts"]["events"]
    config["demo"] = True
    assert feed.status(NOW)["radar"]["status"] == "demo"


def test_matching_current_layover_context_and_deadhead_interruption(setup_feed):
    feed, _ = setup_feed
    inbound = {"origin": "VPS", "destination": "ATL", "start": (NOW - timedelta(hours=4)).isoformat(),
               "end": (NOW - timedelta(hours=2)).isoformat()}
    outbound = {"origin": "ATL", "destination": "JFK", "start": (NOW + timedelta(hours=30)).isoformat(),
                "end": (NOW + timedelta(hours=32)).isoformat()}
    data = {"demo": False, "flights": [inbound, outbound]}
    state, network = poll(feed, snapshot=data)
    assert network.call_count == 4
    assert {row["airport"] for row in state["airport_alerts"]["events"]} == {"ATL"}
    data["flights"].insert(1, {**inbound, "deadhead": True, "start": (NOW - timedelta(hours=1)).isoformat(),
                              "end": (NOW + timedelta(hours=1)).isoformat()})
    assert not any("layover" in row.get("roles", []) for row in select_airports(data, NOW))


@pytest.mark.parametrize("start,end,eligible", [
    ("2026-10-31T09:00:00-05:00", "2026-10-31T11:00:00-05:00", True),
    ("2026-11-01T22:00:00-06:00", "2026-11-01T23:30:00-06:00", True),
    ("2026-11-02T00:01:00-06:00", "2026-11-02T02:00:00-06:00", False),
    ("2026-10-30T23:30:00-05:00", "2026-10-31T00:30:00-05:00", True),
    ("2026-10-30T09:00:00-05:00", "2026-10-30T11:00:00-05:00", False),
])
def test_radar_whole_local_flight_day_and_day_before_across_fall_dst(start, end, eligible):
    data = {"demo": False, "flights": [{"origin": "ATL", "destination": "JFK", "start": start, "end": end}]}
    assert _radar_context(data, {"timezone": "America/Chicago"}, NOW) is eligible


def test_radar_local_midnight_ends_completed_day_and_dst_clock_is_aware():
    data = {"demo": False, "flights": [{"origin": "ATL", "destination": "JFK",
        "start": "2026-11-01T01:30:00-05:00", "end": "2026-11-01T01:45:00-06:00"}]}
    config = {"timezone": "America/Chicago"}
    assert _radar_context(data, config, datetime(2026, 11, 2, 5, 59, 59, tzinfo=UTC))
    assert not _radar_context(data, config, datetime(2026, 11, 2, 6, 0, 0, tzinfo=UTC))


def test_radar_deadheads_require_display_choice_and_invalid_zone_stays_off():
    data = snapshot()
    data["flights"][0]["deadhead"] = True
    assert not _radar_context(data, {"timezone": "America/Chicago", "show_deadheads": False}, NOW)
    assert _radar_context(data, {"timezone": "America/Chicago", "show_deadheads": True}, NOW)
    assert not _radar_context(data, {"timezone": "invalid"}, NOW)
    data["demo"] = True
    assert not _radar_context(data, {"timezone": "America/Chicago", "show_deadheads": True}, NOW)


def test_poll_cadences_are_independent_and_status_is_network_free(setup_feed):
    feed, _ = setup_feed
    with patch("flightwall.flight_environment.time.monotonic", return_value=100):
        poll(feed)
    with patch("flightwall.flight_environment.time.monotonic", return_value=399):
        _, network = poll(feed)
    network.assert_not_called()
    with patch("flightwall.flight_environment.time.monotonic", return_value=400):
        _, network = poll(feed)
    assert [call.args[0] for call in network.call_args_list] == [FAA_URL]
    with patch("flightwall.flight_environment.time.monotonic", return_value=700):
        _, network = poll(feed)
    assert [call.args[0] for call in network.call_args_list] == [radar.CAPABILITIES_URL, FAA_URL]


@pytest.mark.parametrize("minutes,section", [(10, "airport_alerts"), (20, "radar")])
def test_fetch_freshness_end_is_exclusive_per_status_call(setup_feed, minutes, section):
    feed, _ = setup_feed
    poll(feed)
    assert feed.status(NOW + timedelta(minutes=minutes) - timedelta(microseconds=1))[section]["status"] == "ready"
    assert feed.status(NOW + timedelta(minutes=minutes))[section]["status"] == "stale"


@pytest.mark.parametrize("observed", [None, "invalid", "2026-11-01T01:31:00Z", "2026-11-01T00:59:59Z"])
def test_radar_cannot_relabel_missing_future_or_old_source_as_current(setup_feed, observed):
    feed, _ = setup_feed
    state, _ = poll(feed, observed_at=observed)
    assert state["radar"]["status"] == "unavailable"
    assert not state["radar"]["image_available"]


@pytest.mark.parametrize("clock,status", [("Sun Nov 1 01:31:00 2026 GMT", "stale"),
    ("Sun Nov 1 01:14:59 2026 GMT", "stale"), ("not-a-date", "unavailable")])
def test_faa_requires_current_source_time_not_just_successful_fetch(setup_feed, clock, status):
    feed, _ = setup_feed
    state, _ = poll(feed, faa=faa_bytes(clock))
    assert state["airport_alerts"]["status"] == status


def test_clock_rollback_and_failed_refresh_never_claim_ready(setup_feed):
    feed, _ = setup_feed
    with patch("flightwall.flight_environment.time.monotonic", return_value=100):
        poll(feed)
    assert {row["status"] for row in feed.status(NOW - timedelta(seconds=1)).values()} == {"stale"}
    with patch("flightwall.flight_environment.time.monotonic", return_value=700), \
         patch("flightwall.flight_environment.request_bytes", side_effect=OSError("PRIVATE_NETWORK_ERROR")):
        state = feed.poll(snapshot(), NOW + timedelta(minutes=1))
    assert {row["status"] for row in state.values()} == {"stale"}
    assert "PRIVATE_NETWORK_ERROR" not in json.dumps(state) + feed.path.read_text()


def test_private_cache_restart_is_dated_and_context_is_not_persisted(setup_feed):
    feed, config = setup_feed
    with patch("flightwall.flight_environment.time.monotonic", return_value=100):
        poll(feed)
    restarted = FlightEnvironmentFeed(feed.path.parent, lambda: config)
    assert restarted.status(NOW)["radar"]["status"] == "demo"
    with patch("flightwall.flight_environment.request_bytes", side_effect=OSError):
        state = restarted.poll(snapshot(), NOW)
    assert state["radar"]["status"] == "stale"
    assert state["radar"]["image_available"]
    assert state["airport_alerts"]["status"] == "stale"


def test_radar_only_first_frame_rgb_and_size_bounded():
    data = BytesIO()
    Image.new("RGB", (4000, 2000), "red").save(data, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (4000, 2000), "blue")], duration=100)
    with Image.open(BytesIO(_radar_png(data.getvalue()))) as image:
        assert image.mode == "RGB" and image.size == (2048, 1024)
        assert image.getpixel((100, 100)) == (255, 0, 0)


@pytest.mark.parametrize("raw", [b"not an image", image_bytes(format="JPEG"), image_bytes((4000, 3000))])
def test_radar_invalid_or_excessive_image_is_rejected(raw):
    with pytest.raises((ValueError, OSError)):
        _radar_png(raw)


@pytest.mark.parametrize("raw", [
    b'<!DOCTYPE AIRPORT_STATUS_INFORMATION SYSTEM "https://untrusted.invalid/schema"><AIRPORT_STATUS_INFORMATION/>',
    b'<!DOCTYPE a [<!ENTITY x "EXPAND">]><a>&x;</a>',
    '<!DOCTYPE a [<!ENTITY x "EXPAND">]><a>&x;</a>'.encode("utf-16"),
    b'<wrong/>', b'<AIRPORT_STATUS_INFORMATION>',
])
def test_xml_external_entities_unknown_root_and_malformed_data_rejected(raw):
    with pytest.raises((ValueError, ET.ParseError)):
        _parse_faa(raw)


def test_faa_no_programs_is_a_dated_empty_list_not_airline_closure():
    raw = b'<AIRPORT_STATUS_INFORMATION><Update_Time>Sun Nov 1 01:30:00 2026 GMT</Update_Time><Delay_type><Airport_Closure_List><Airport><ARPT>ATL</ARPT><Reason>GA ONLY</Reason></Airport></Airport_Closure_List></Delay_type></AIRPORT_STATUS_INFORMATION>'
    assert _parse_faa(raw)["events"] == []


def test_source_reason_is_bounded_plain_text_without_terminal_controls():
    raw = faa_bytes().replace(b"WEATHER / THUNDERSTORMS", b"&lt;b&gt;WIND&lt;/b&gt; &#27; OTHER")
    # XML disallows some control entities outright; valid text is sanitized too.
    with pytest.raises(ET.ParseError):
        _parse_faa(raw)
    raw = faa_bytes().replace(b"WEATHER / THUNDERSTORMS", b"&lt;b&gt;WIND&lt;/b&gt; &#133; OTHER")
    assert _parse_faa(raw)["events"][0]["reason"] == "WIND OTHER"


class Response:
    def __init__(self, url, raw=b"public", modified=None, encoding=None):
        self.url, self.raw = url, raw
        self.headers = {"Last-Modified": modified}
        if encoding:
            self.headers["Content-Encoding"] = encoding
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def geturl(self): return self.url
    def read(self, limit): return self.raw[:limit]


@pytest.mark.parametrize("url", ["http://nasstatus.faa.gov/api/airport-status-information", FAA_URL + "?airport=ATL", RADAR_URL + "#fragment", "https://untrusted.invalid/radar.gif"])
def test_only_fixed_public_endpoints_permitted(url):
    with patch("urllib.request.build_opener") as network, pytest.raises(ValueError):
        request_bytes(url)
    network.assert_not_called()


def test_request_has_no_proxy_redirect_or_private_query():
    opener = Mock()
    opener.open.return_value = Response(RADAR_URL, b"public", "Sun, 01 Nov 2026 01:30:00 GMT")
    with patch("urllib.request.build_opener", return_value=opener) as factory:
        raw, modified = request_bytes(RADAR_URL)
    assert raw == b"public" and modified.endswith("GMT")
    proxy, no_redirect = factory.call_args.args
    assert proxy.proxies == {}
    assert no_redirect.redirect_request(None, None, None, None, None, None) is None
    request = opener.open.call_args.args[0]
    assert request.full_url == RADAR_URL and opener.open.call_args.kwargs["timeout"] == 10
    assert "PRIVATE" not in str(request.headers)


@pytest.mark.parametrize("response", [Response("https://untrusted.invalid/radar.gif"),
    Response(RADAR_URL, b"x" * (MAX_RESPONSE[RADAR_URL] + 1)), Response(RADAR_URL, encoding="gzip")])
def test_redirect_oversized_or_encoded_body_rejected(response):
    opener = Mock()
    opener.open.return_value = response
    with patch("urllib.request.build_opener", return_value=opener), pytest.raises(ValueError):
        request_bytes(RADAR_URL)
