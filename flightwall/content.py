"""Opt-in inspiration, artwork, source-dated research, and safe layover cards."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from PIL import Image, ImageOps

from .config import private_json
from .feeds import parse_time
from .personal import PersonalError, PersonalStore

UTC = timezone.utc
MAX_GENERATED_CARDS = 12


def _is_generated(card):
    return isinstance(card, dict) and card.get("ai_generated") is True


def _retained_cards(cards, generated_limit=MAX_GENERATED_CARDS):
    """Keep manual choices and only the newest automatic ideas in save order."""
    generated = [index for index, card in enumerate(cards) if _is_generated(card)]
    keep = set(generated[-generated_limit:]) if generated_limit > 0 else set()
    return [card for index, card in enumerate(cards) if not _is_generated(card) or index in keep]


def _now():
    return datetime.now(UTC)


def _text(value, limit=600):
    if not isinstance(value, str):
        raise ValueError("Use plain text for the card")
    value = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())
    if not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"Use 1 to {limit} plain-text characters")
    return value


def _external_text(value, limit=160):
    """External headlines may be long; a long title must not discard a feed."""
    if not isinstance(value, str):
        raise ValueError("The source did not supply a text title")
    value = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value[:20000])).split())
    value = "".join(char for char in value if not unicodedata.category(char).startswith("C"))
    if not value:
        raise ValueError("The source title is empty")
    return value if len(value) <= limit else value[:limit-3].rstrip() + "..."


def _url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError("Use a public HTTPS source URL")
    parts = urllib.parse.urlsplit(value)
    try:
        parts.port
    except ValueError:
        raise ValueError("Use a valid HTTPS source port") from None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError("Use a public HTTPS source URL without credentials")
    host = parts.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Source feeds must be public HTTPS addresses")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Reject ambiguous numeric forms that network libraries can interpret
        # as loopback, including 127.1, octal forms and integer/hex addresses.
        if "." not in host or re.fullmatch(r"[0-9.]+", host) or host.startswith("0x"):
            raise ValueError("Source feeds must be public HTTPS addresses") from None
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or ".." in host:
            raise ValueError("Source feeds must use a public HTTPS hostname") from None
    else:
        if not address.is_global:
            raise ValueError("Source feeds must be public HTTPS addresses")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _request(url, *, payload=None, token=None, max_bytes=1_000_000):
    _url(url)
    request = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
        headers={"User-Agent": "FlightWall/0.3.0", "Accept": "application/json, application/rss+xml, application/atom+xml",
                 **({"Content-Type": "application/json"} if payload is not None else {}),
                 **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=15) as response:
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("The source response was too large")
        return raw


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        return parse_time(value).astimezone(UTC).isoformat()
    except ValueError:
        pass
    try:
        result = parsedate_to_datetime(value)
        if result.tzinfo:
            return result.astimezone(UTC).isoformat()
    except (ValueError, TypeError):
        pass
    for pattern in ("%Y-%m-%d", "%Y %b %d"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=UTC).isoformat()
        except ValueError:
            pass
    return None


class SourceContent:
    def __init__(self, data_dir, config_getter):
        self.data_dir = Path(data_dir)
        self.config_getter = config_getter
        self.path = self.data_dir / "content.json"
        self.research_path = self.data_dir / "research-cache.json"
        self.feeds_path = self.data_dir / "inspiration-cache.json"
        self.layover_path = self.data_dir / "layovers.json"
        self.lock = threading.RLock()
        self._generation_storage = PersonalStore(data_dir, config_getter)
        self._generation_storage.lock_path = self.data_dir / "openai-usage.lock"

    @staticmethod
    def _read(path, default):
        try:
            if path.stat().st_size > 4_000_000:
                return default
            def reject_constant(_value):
                raise ValueError("Nonfinite JSON")
            return json.loads(path.read_text(), parse_constant=reject_constant)
        except (OSError, ValueError):
            return default

    def add_card(self, payload, *, generated=False):
        if not isinstance(payload, dict) or set(payload) - {"owner", "kind", "title", "body", "source_url", "image_data"}:
            raise ValueError("Provide an inspiration title, text, optional source or artwork")
        owner, kind = payload.get("owner", "lisa"), payload.get("kind", "inspiration")
        if not isinstance(owner, str) or not isinstance(kind, str) or owner not in {"lisa", "public"} or kind not in {"inspiration", "artwork"}:
            raise ValueError("Choose a Lisa or public inspiration/artwork card")
        identifier = secrets.token_hex(12)
        card = {"id": identifier, "owner": owner, "profile": owner, "kind": "inspiration",
                "title": _text(payload.get("title"), 160), "body": _text(payload.get("body") or "Your inspiration board", 600),
                "source": "OpenAI / generated idea" if generated else "Your board", "observed_at": _now().isoformat()}
        if generated:
            card["ai_generated"] = True
        if payload.get("source_url"):
            card["source_url"] = _url(payload["source_url"])
        if kind == "artwork":
            raw = payload.get("image_data")
            if not isinstance(raw, str) or len(raw) > 7_000_000 or not re.match(r"^data:image/(?:png|jpeg);base64,", raw):
                raise ValueError("Upload a PNG or JPEG smaller than 5 MB")
            try:
                data = base64.b64decode(raw.split(",", 1)[1], validate=True)
                if len(data) > 5_000_000:
                    raise ValueError()
                with Image.open(io.BytesIO(data)) as source:
                    if source.format not in {"PNG", "JPEG"} or source.width * source.height > 16_000_000:
                        raise ValueError()
                    thumbnail = ImageOps.contain(ImageOps.exif_transpose(source).convert("RGB"), (512, 256))
                    image = Image.new("RGB", (512, 256), (3, 9, 13))
                    image.paste(thumbnail, ((512-thumbnail.width)//2, (256-thumbnail.height)//2))
            except Exception:
                raise ValueError("Use a valid PNG or JPEG up to 16 megapixels and 5 MB") from None
            card["image_id"] = identifier
        with self.lock:
            cards = self._read(self.path, [])
            if not isinstance(cards, list):
                raise ValueError("The saved board needs repair before another card can be added")
            manual_count = sum(not _is_generated(existing) for existing in cards)
            if manual_count >= 100:
                raise ValueError("The board already has 100 saved cards")
            # Manual choices have priority over disposable generated ideas.
            # Reserve one slot for this new card, then trim only generated ones.
            generated_slots = min(MAX_GENERATED_CARDS - int(generated), 99 - manual_count)
            cards = _retained_cards(cards, generated_slots)
            filename = None
            try:
                if kind == "artwork":
                    directory = self.data_dir / "artwork"
                    if directory.is_symlink():
                        raise ValueError("The artwork folder must be a local directory")
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    directory.chmod(0o700)
                    filename = directory / (identifier + ".png")
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(filename, flags, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        image.save(stream, format="PNG")
                        stream.flush()
                        os.fsync(stream.fileno())
                private_json(self.path, cards + [card])
            except Exception:
                if filename is not None:
                    filename.unlink(missing_ok=True)
                raise
        return {"saved": True, "id": identifier}

    def image_data(self, card):
        if not isinstance(card, dict):
            return None
        identifier = card.get("image_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{24}", identifier):
            return None
        try:
            path = self.data_dir / "artwork" / (identifier + ".png")
            metadata = path.lstat()
            if path.parent.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 800_000:
                return None
            return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
        except OSError:
            return None

    def status(self):
        config = self.config_getter()
        cards = self._read(self.path, [])
        feeds = self._read(self.feeds_path, {})
        health = self._read(self.research_path, {})
        topics_hash = hashlib.sha256(str(config.get("health_query", "")).encode()).hexdigest()
        research = health.get("cards", []) if isinstance(health, dict) and health.get("selection") == topics_hash else []
        feed_key = hashlib.sha256(str(config.get("inspiration_sources", "[]")).encode()).hexdigest()
        source_cards = feeds.get("cards", []) if isinstance(feeds, dict) and feeds.get("selection") == feed_key else []
        cards = _retained_cards([card for card in cards if isinstance(card, dict)]) if isinstance(cards, list) else []
        source_cards = [card for card in source_cards if isinstance(card, dict)] if isinstance(source_cards, list) else []
        research = [card for card in research if isinstance(card, dict)] if isinstance(research, list) else []
        now = _now()
        fresh_sources = []
        for card in source_cards:
            try:
                if parse_time(card["expires_at"]) > now:
                    fresh_sources.append(card)
            except (KeyError, ValueError, TypeError):
                continue
        fresh_research = []
        for card in research:
            try:
                published = parse_time(card["published_at"])
                fetched = parse_time(card["fetched_at"])
                _url(card["source_url"])
                if card.get("source") in {"PubMed", "ClinicalTrials.gov"} and published <= now + timedelta(minutes=5) and timedelta(minutes=-5) <= now-fetched <= timedelta(hours=config.get("health_max_age_hours", 168)):
                    fresh_research.append(card)
            except (KeyError, ValueError, TypeError):
                continue
        inspiration = cards + fresh_sources
        def newest(entry):
            index, card = entry
            try:
                stamp = parse_time(card.get("observed_at"))
            except (ValueError, TypeError):
                stamp = datetime.min.replace(tzinfo=UTC)
            return stamp, index
        inspiration = [card for _index, card in sorted(enumerate(inspiration), key=newest, reverse=True)]
        return {"inspiration": inspiration[:20] if config.get("inspiration_enabled") else [],
                "health": fresh_research[:8] if config.get("health_enabled") else [],
                "research_last_sync": health.get("last_sync") if isinstance(health, dict) else None,
                "research_error": health.get("error") if isinstance(health, dict) else None,
                "inspiration_error": feeds.get("error") if isinstance(feeds, dict) else None}

    def refresh(self):
        config = self.config_getter()
        if config.get("inspiration_enabled"):
            self._refresh_inspiration(config)
        if config.get("health_enabled") and config.get("health_query", "").strip():
            self._refresh_research(config)

    def _refresh_inspiration(self, config):
        cards, error = [], None
        try:
            urls = json.loads(config.get("inspiration_sources", "[]"))
            for url in urls[:5]:
                raw = _request(_url(url))
                if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                    raise ValueError("Unsupported XML declaration")
                root = ET.fromstring(raw)
                items = list(root.findall("./channel/item")) + list(root.findall("{http://www.w3.org/2005/Atom}entry"))
                for item in items[:3]:
                    def value(name):
                        return item.findtext(name) or item.findtext("{http://www.w3.org/2005/Atom}" + name) or ""
                    title = _external_text(value("title"), 160)
                    link = value("link")
                    if not link:
                        node = item.find("{http://www.w3.org/2005/Atom}link")
                        link = node.get("href", "") if node is not None else url
                    link = _url(link or url)
                    cards.append({"id": hashlib.sha256(link.encode()).hexdigest()[:24], "kind": "inspiration",
                        "owner": "lisa", "profile": "lisa", "title": title, "body": "A saved design or art source to explore.",
                        "source": urllib.parse.urlsplit(url).hostname, "source_url": link,
                        "published_at": _date(value("pubDate") or value("published") or value("updated")),
                        "observed_at": _now().isoformat(), "expires_at": (_now()+timedelta(days=7)).isoformat()})
        except Exception:
            error = "An inspiration source could not be refreshed. Check the saved feed URLs."
        private_json(self.feeds_path, {"selection": hashlib.sha256(str(config.get("inspiration_sources", "[]")).encode()).hexdigest(),
                                      "cards": cards[:12], "last_sync": _now().isoformat(), "error": error})

    def _refresh_research(self, config):
        query = config["health_query"].strip()[:300]
        cards, errors = [], []
        now = _now()
        try:
            params = urllib.parse.urlencode({"query.cond": query, "pageSize": 4, "sort": "LastUpdatePostDate:desc", "format": "json",
                "fields": "NCTId,BriefTitle,OverallStatus,Phase,LastUpdatePostDate"})
            result = json.loads(_request("https://clinicaltrials.gov/api/v2/studies?" + params))
            for study in result.get("studies", [])[:4]:
                protocol = study.get("protocolSection", {})
                identity, status = protocol.get("identificationModule", {}), protocol.get("statusModule", {})
                nct = identity.get("nctId", "")
                updated = status.get("lastUpdatePostDateStruct", {}).get("date")
                stamp = _date(updated)
                if not re.fullmatch(r"NCT[0-9]{8}", nct) or not stamp:
                    continue
                phases = protocol.get("designModule", {}).get("phases", [])
                body = "Study registration / " + str(status.get("overallStatus", "Status unavailable")).replace("_", " ")
                if phases:
                    body += " / " + ", ".join(str(p).replace("_", " ") for p in phases)
                cards.append({"id": nct, "owner": "lisa", "profile": "lisa", "kind": "health",
                    "title": _external_text(identity.get("briefTitle"), 160), "body": body,
                    "source": "ClinicalTrials.gov", "source_url": "https://clinicaltrials.gov/study/" + nct,
                    "published_at": stamp, "source_date": stamp, "observed_at": now.isoformat(), "fetched_at": now.isoformat()})
        except Exception:
            errors.append("ClinicalTrials.gov could not be refreshed.")
        try:
            params = urllib.parse.urlencode({"db": "pubmed", "retmode": "json", "retmax": 4, "sort": "pub_date", "term": query})
            search = json.loads(_request("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + params))
            ids = search.get("esearchresult", {}).get("idlist", [])[:4]
            ids = [value for value in ids if isinstance(value, str) and re.fullmatch(r"[0-9]+", value)]
            if ids:
                params = urllib.parse.urlencode({"db": "pubmed", "retmode": "json", "id": ",".join(ids)})
                summaries = json.loads(_request("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + params)).get("result", {})
                for identifier in ids:
                    record = summaries.get(identifier, {})
                    stamp = _date(record.get("pubdate"))
                    if not stamp:
                        continue
                    cards.append({"id": "pubmed-" + identifier, "owner": "lisa", "profile": "lisa", "kind": "health",
                        "title": _external_text(record.get("title"), 160), "body": "Research publication / " + _external_text(record.get("source") or "PubMed", 120),
                        "source": "PubMed", "source_url": "https://pubmed.ncbi.nlm.nih.gov/" + identifier + "/",
                        "published_at": stamp, "source_date": stamp, "observed_at": now.isoformat(), "fetched_at": now.isoformat()})
        except Exception:
            errors.append("PubMed could not be refreshed.")
        private_json(self.research_path, {"selection": hashlib.sha256(str(config.get("health_query", "")).encode()).hexdigest(),
                                         "cards": cards[:8], "last_sync": now.isoformat(), "error": " ".join(errors) or None})

    def generate_inspiration(self):
        config = self.config_getter()
        topics = config.get("inspiration_topics", "").strip()
        if not config.get("openai_enabled") or not config.get("inspiration_enabled") or not topics:
            return {"generated": False, "message": "Enable inspiration and OpenAI, then enter design interests first."}
        with self.lock, self._generation_lock():
            now = _now()
            existing = self._read(self.path, [])
            if not isinstance(existing, list) or sum(not _is_generated(card) for card in existing) >= 100:
                return {"generated": False, "message": "The board has 100 manually saved cards or needs repair. No billed generation request was made."}
            usage_path = self.data_dir / "openai-usage.json"
            try:
                usage = self._read(usage_path, None)
                if usage_path.exists():
                    if not isinstance(usage, dict) or set(usage) != {"date", "count"} or isinstance(usage["count"], bool) or not isinstance(usage["count"], int) or usage["count"] < 0:
                        raise ValueError()
                    day = datetime.strptime(usage["date"], "%Y-%m-%d").date()
                    if day > now.date():
                        raise ValueError()
                count = usage["count"] if usage and usage["date"] == now.date().isoformat() else 0
            except (ValueError, TypeError, KeyError):
                return {"generated": False, "message": "The local generation counter could not be verified. Repair it in setup before making another billed request."}
            limit = config.get("openai_daily_limit", 5)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100 or count >= limit:
                return {"generated": False, "message": "The local daily generation limit has been reached."}
            key_path = self.data_dir / "openai.key"
            try:
                if key_path.stat().st_size > 4096:
                    raise ValueError()
                key = key_path.read_text().strip()
                if not key or any(char.isspace() for char in key):
                    raise ValueError()
            except (OSError, ValueError):
                return {"generated": False, "message": "Save your OpenAI API key in setup first."}
            private_json(usage_path, {"date": now.date().isoformat(), "count": count+1})
            payload = {"model": config.get("openai_model", "gpt-5-mini"), "store": False,
                "instructions": "Write one short, practical art or interior-design exercise. Use only the interests supplied. Do not infer private facts, discuss health, or mention current news. At most 50 words.",
                "input": topics[:600], "max_output_tokens": 1200}
            try:
                result = json.loads(_request("https://api.openai.com/v1/responses", payload=payload, token=key))
                text = " ".join(part.get("text", "") for output in result.get("output", []) if output.get("type") == "message"
                                for part in output.get("content", []) if part.get("type") == "output_text")
                body = _text(text, 600)
            except Exception:
                return {"generated": False, "message": "OpenAI could not create a design prompt. Check the API account, model and key."}
            saved = self.add_card({"owner": "lisa", "kind": "inspiration", "title": "A creative idea to try", "body": body}, generated=True)
            return {"generated": True, **saved}

    @contextmanager
    def _generation_lock(self):
        """Share the billed-call cap between the service and setup processes."""
        try:
            with self._generation_storage._locked():
                yield
        except PersonalError:
            raise ValueError("Generation storage is unavailable; check local permissions.") from None

    def add_layover(self, payload):
        if not isinstance(payload, dict) or set(payload)-{"airport", "start", "end", "hotel", "city"}:
            raise ValueError("Use airport, start, end, optional hotel and city")
        airport = str(payload.get("airport", "")).upper()
        if not re.fullmatch(r"[A-Z]{3}", airport):
            raise ValueError("Use a three-letter airport code")
        start, end = parse_time(payload.get("start")), parse_time(payload.get("end"))
        if end <= start or end-start > timedelta(days=14):
            raise ValueError("A layover must end after it starts, within fourteen days")
        item = {"id": secrets.token_hex(12), "airport": airport, "start": start.isoformat(), "end": end.isoformat(),
                "city": _text(payload.get("city") or airport, 100), "hotel_user_provided": True, "source": "Your trip details"}
        if payload.get("hotel"):
            item["hotel"] = _text(payload["hotel"], 160)
        with self.lock:
            items = self._read(self.layover_path, [])
            valid = []
            for existing in items if isinstance(items, list) else []:
                try:
                    if isinstance(existing, dict) and parse_time(existing["end"]) > _now():
                        valid.append(existing)
                except (ValueError, TypeError, KeyError):
                    continue
            private_json(self.layover_path, (valid + [item])[-100:])
        return {"saved": True, "id": item["id"]}

    def layovers(self, snapshot, now=None):
        now = now or _now()
        manual = self._read(self.layover_path, [])
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        scheduled_layovers = snapshot.get("layovers")
        scheduled_layovers = scheduled_layovers if isinstance(scheduled_layovers, list) else []
        result = []
        for item in (manual if isinstance(manual, list) else []) + scheduled_layovers:
            try:
                if parse_time(item["start"]) <= now < parse_time(item["end"]):
                    result.append(dict(item))
            except (ValueError, TypeError, KeyError):
                continue
        flights = []
        scheduled_flights = snapshot.get("flights")
        for flight in scheduled_flights if isinstance(scheduled_flights, list) else []:
            try:
                if isinstance(flight, dict) and parse_time(flight["end"]) > parse_time(flight["start"]):
                    flights.append(flight)
            except (ValueError, TypeError, KeyError):
                continue
        flights.sort(key=lambda flight: parse_time(flight["start"]))
        for previous, following in zip(flights, flights[1:]):
            try:
                start, end = parse_time(previous["end"]), parse_time(following["start"])
                airport = previous["destination"]
                if airport == following["origin"] and timedelta(hours=4) <= end-start <= timedelta(days=5) and start <= now < end:
                    if not any(item.get("airport") == airport for item in result):
                        result.append({"id": "gap-" + str(previous["id"]), "airport": airport, "start": start.isoformat(),
                                       "end": end.isoformat(), "source": "Calendar schedule / inferred layover", "hotel": ""})
            except (ValueError, KeyError, TypeError):
                continue
        if not self.config_getter().get("hotel_details_enabled"):
            result = [{key: value for key, value in item.items() if key not in {"hotel", "hotel_user_provided"}} for item in result]
        return result
