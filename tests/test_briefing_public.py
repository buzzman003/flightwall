from datetime import datetime, timezone
from urllib.error import URLError

from flightwall import briefing_public as briefing


NOW = datetime(2026, 11, 28, 1, tzinfo=timezone.utc)


def test_headlines_keep_recent_source_dates_when_another_feed_fails(monkeypatch):
    xml = """<rss version="2.0"><channel>
      <item><title>Fresh &amp; useful</title><pubDate>Sat, 28 Nov 2026 00:42:00 GMT</pubDate></item>
      <item><title>Fresh &amp; useful</title><pubDate>Sat, 28 Nov 2026 00:38:00 GMT</pubDate></item>
      <item><title>Undated article</title></item>
      <item><title>Future date</title><pubDate>Sat, 28 Nov 2026 02:00:00 GMT</pubDate></item>
      <item><title>Old article</title><pubDate>Wed, 25 Nov 2026 12:00:00 GMT</pubDate></item>
    </channel></rss>""".encode()

    def request(url, *, max_bytes):
        assert max_bytes == briefing.MAX_BYTES
        if url == briefing.NEWS_SOURCES[0][1]:
            return xml
        raise URLError("Feed unavailable")

    monkeypatch.setattr(briefing, "_request", request)
    assert briefing.fetch_headlines(NOW, "America/Chicago") == [
        {"title": "Fresh & useful", "source": "NPR", "date": "2026-11-27"}
    ]


def test_headlines_ignore_xml_entities_and_limit_rotation(monkeypatch):
    rows = "".join(
        f"<item><title>Headline {i}</title><pubDate>Sat, 28 Nov 2026 00:{i:02}:00 GMT</pubDate></item>"
        for i in range(9)
    )

    def request(url, **_):
        if url == briefing.NEWS_SOURCES[0][1]:
            return b'<!DOCTYPE rss [<!ENTITY text "do not expand">]><rss><channel/></rss>'
        return f"<rss><channel>{rows}</channel></rss>".encode()

    monkeypatch.setattr(briefing, "_request", request)
    result = briefing.fetch_headlines(NOW, "America/Chicago")
    assert [row["title"] for row in result] == [f"Headline {i}" for i in range(8, 2, -1)]
    assert all(row["source"] == "BBC News" for row in result)


def test_event_dates_skip_canceled_past_and_invalid_events_and_use_city_civil_date(monkeypatch):
    ics = b"""BEGIN:VCALENDAR
BEGIN:VTIMEZONE
DTSTART:20261127T010000
END:VTIMEZONE
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20261128T001500
SUMMARY:Market\\, lights
  and music
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261130
SUMMARY:Public workshop
END:VEVENT
BEGIN:VEVENT
DTSTART:20261201T003000Z
SUMMARY:Arts night
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20261126T100000
SUMMARY:Earlier event
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261131
SUMMARY:Invalid late November
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261202
SUMMARY:Canceled celebration
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261208
SUMMARY:Too far ahead
END:VEVENT
END:VCALENDAR
"""
    monkeypatch.setattr(briefing, "EVENT_SOURCES", (briefing.EVENT_SOURCES[1],))
    monkeypatch.setattr(briefing, "_request", lambda url, **_: ics)
    assert briefing.fetch_events(NOW, "America/Chicago") == [
        {"title": "Market, lights and music", "source": "Fort Walton Beach", "date": "2026-11-28"},
        {"title": "Public workshop", "source": "Fort Walton Beach", "date": "2026-11-30"},
        {"title": "Arts night", "source": "Fort Walton Beach", "date": "2026-11-30"},
    ]
