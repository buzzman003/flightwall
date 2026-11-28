"""Public headlines and local civil dates from the publishers' feeds."""
from datetime import datetime, timedelta
from http.client import HTTPException
import re
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from .content import _date, _external_text, _request


NEWS_SOURCES = (
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
)
EVENT_SOURCES = (
    ("Destin Parks & Recreation",
     "https://www.cityofdestin.com/common/modules/iCalendar/iCalendar.aspx?catID=26&feed=calendar"),
    ("Fort Walton Beach",
     "https://www.fwb.org/common/modules/iCalendar/iCalendar.aspx?catID=24&feed=calendar"),
)
REGIONAL_ZONE = ZoneInfo("America/Chicago")
MAX_BYTES = 256_000
MAX_ROWS = 6
HEADLINE_MAX_AGE = timedelta(days=2)
EVENT_HORIZON = timedelta(days=7)
_VEVENT = re.compile(r"^BEGIN:VEVENT\r?\n(.*?)^END:VEVENT\r?$", re.M | re.S)
_ICAL_START = re.compile(r"\d{8}(?:T\d{6}Z?)?")


def _headlines(raw, source, now, zone):
    feed = raw.decode("utf-8-sig")
    if "<!DOCTYPE" in feed.upper() or "<!ENTITY" in feed.upper():
        raise ValueError("Unsupported RSS declaration")
    root = ET.fromstring(feed)
    for item in root.findall("./channel/item")[:50]:
        published = _date(item.findtext("pubDate"))
        if not published:
            continue
        stamp = datetime.fromisoformat(published)
        if not now - HEADLINE_MAX_AGE <= stamp <= now:
            continue
        try:
            title = _external_text(item.findtext("title"))
        except ValueError:
            continue
        yield stamp, {"title": title, "source": source, "date": stamp.astimezone(zone).date().isoformat()}


def _event_day(value):
    if not _ICAL_START.fullmatch(value):
        raise ValueError("Missing calendar start")
    if len(value) == 8:
        return datetime.strptime(value, "%Y%m%d").date()
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%S%z").astimezone(REGIONAL_ZONE).date()
    # The city's published civil date is unambiguous even when its feed
    # labels the local clock with the wrong TZID. We display no clock.
    return datetime.strptime(value, "%Y%m%dT%H%M%S").date()


def _events(raw, source, now, zone):
    start = now.astimezone(zone).date()
    unfolded = re.sub(r"\r?\n[ \t]", "", raw.decode("utf-8-sig"))
    for entry in _VEVENT.finditer(unfolded):
        values = {}
        for line in entry[1].splitlines():
            prop, sep, value = line.partition(":")
            key = prop.split(";", 1)[0].upper()
            if sep and key in {"SUMMARY", "DTSTART", "STATUS"}:
                values[key] = value
        if values.get("STATUS", "").strip().upper() in {"CANCELLED", "CANCELED"}:
            continue
        try:
            day = _event_day(values["DTSTART"])
            title = _external_text(re.sub(r"\\([nN,;\\\"])", lambda m: " " if m[1].lower() == "n" else m[1],
                                          values["SUMMARY"]))
        except (KeyError, ValueError):
            continue
        if start <= day <= start + EVENT_HORIZON:
            yield day, {"title": title, "source": source, "date": day.isoformat()}


def _fetch(sources, parse, now, timezone_name, *, newest_first=False):
    zone = ZoneInfo(timezone_name)
    results = []
    for source, url in sources:
        try:
            results.extend(parse(_request(url, max_bytes=MAX_BYTES), source, now, zone))
        except (OSError, HTTPException, ValueError, ET.ParseError):
            continue
    rows, seen = [], set()
    for _, row in sorted(results, key=lambda entry: entry[0], reverse=newest_first):
        title = row["title"].casefold()
        key = title if newest_first else (row["date"], title)
        if key not in seen:
            rows.append(row)
            seen.add(key)
        if len(rows) >= MAX_ROWS:
            break
    return rows


def fetch_headlines(now, timezone_name):
    """Return up to six recent, dated headlines with source attribution."""
    return _fetch(NEWS_SOURCES, _headlines, now, timezone_name, newest_first=True)


def fetch_events(now, timezone_name):
    """Return the next local public events over the coming week."""
    return _fetch(EVENT_SOURCES, _events, now, timezone_name)
