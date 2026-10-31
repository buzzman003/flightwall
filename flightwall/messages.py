"""Explicitly selected, read-only mail summaries and opt-in phone notifications.

This module never decides who may see a summary. The backend applies household
presence/privacy policy before putting any item into its public status response.
It never sends mail or reads message bodies, attachments, or a phone's inbox.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import getaddresses
import hashlib
import hmac
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import private_json
from .personal import PersonalError, PersonalStore

UTC = timezone.utc
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
PROFILES = frozenset({"chris", "lisa"})
RENTAL_DOMAINS = {"airbnb.com": "Airbnb", "vrbo.com": "VRBO", "homeaway.com": "VRBO"}
CHRIS_NOTIFICATION_APPS = frozenset({"airbnb", "vrbo", "homeaway", "com.airbnb.android", "com.vrbo.android"})
_PUBLIC_MAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com"})
MAX_ITEMS = 20
MAX_AGE = timedelta(hours=24)
MAX_RESPONSE = 2 * 1024 * 1024
_ITEM_KEYS = {"id", "kind", "owner", "title", "body", "source", "observed_at", "expires_at", "needs_reply", "reply_label", "category", "filtered"}


class MessageError(ValueError):
    """A message safe to show without disclosing account details or credentials."""


def _now():
    return datetime.now(UTC)


def _profile(value):
    if not isinstance(value, str) or value not in PROFILES:
        raise MessageError("Choose the Chris or Lisa profile.")
    return value


def _stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise MessageError("Use a timestamp with a timezone, such as 2026-11-03T16:25:00Z.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        raise MessageError("Use a timestamp with a timezone, such as 2026-11-03T16:25:00Z.") from None


def _iso(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    value = html.unescape(value[:8192])
    parser = _PlainText()
    parser.feed(value)
    plain = "".join(parser.parts)
    plain = "".join(" " if char.isspace() else char for char in plain if char.isspace() or not unicodedata.category(char).startswith("C"))
    return " ".join(plain.split())[:max(0, limit)]


def _list(config, key):
    value = config.get(key, "[]")
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        raise MessageError("The selected sender or app list is invalid. Repair it in setup.") from None
    if not isinstance(result, list) or len(result) > 100 or any(not isinstance(item, str) or not 1 <= len(item) <= 254 for item in result):
        raise MessageError("Choose a bounded list of sender addresses, domains or notification apps in setup.")
    return result


def _domain(value):
    value = value.strip().casefold().rstrip(".")
    if len(value) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value) or "." not in value or ".." in value:
        raise MessageError("Use a complete sender domain such as example-law-firm.com.")
    if any(not part or len(part) > 63 or part.startswith("-") or part.endswith("-") for part in value.split(".")):
        raise MessageError("Use a valid sender domain.")
    return value


def _address(value):
    if not isinstance(value, str) or len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", value.strip()):
        raise MessageError("Use an exact sender email address such as name@example.com.")
    local, domain = value.strip().rsplit("@", 1)
    return local.casefold() + "@" + _domain(domain)


def _rules(config, profile):
    prefix = "chris_lawyer" if profile == "chris" else "lisa_mail"
    addresses = sorted({_address(value) for value in _list(config, prefix + "_senders")})
    domains = sorted({_domain(value) for value in _list(config, prefix + "_domains")})
    if profile == "chris" and any(value in _PUBLIC_MAIL_DOMAINS for value in domains):
        raise MessageError("Use an exact lawyer email address for a public mail service such as Gmail or Outlook; its whole domain is not a lawyer-only filter.")
    return addresses, domains


def _selection(config, profile):
    addresses, domains = _rules(config, profile)
    return hashlib.sha256(json.dumps([profile, addresses, domains], separators=(",", ":")).encode()).hexdigest()


def _category(address, config, profile):
    domain = address.rsplit("@", 1)[-1]
    if profile == "chris":
        for allowed, category in RENTAL_DOMAINS.items():
            if domain == allowed or domain.endswith("." + allowed):
                return category
    addresses, domains = _rules(config, profile)
    if address in addresses or any(domain == allowed or domain.endswith("." + allowed) for allowed in domains):
        return "Lawyer" if profile == "chris" else "Selected mail"
    return None


def _preview_limit(config):
    value = config.get("message_preview_chars", 180)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 300:
        raise MessageError("Set the preview length to a whole number between 0 and 300.")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GmailClient:
    """Fixed-endpoint Gmail read-only client; OAuth consent is owned by setup."""
    def __init__(self, account_dir):
        self.account_dir = Path(account_dir)
        self.client_path = self.account_dir / "gmail-client.json"
        self.token_path = self.account_dir / "gmail-token.json"
        self.opener = build_opener(_NoRedirect())

    @staticmethod
    def _json(path):
        with path.open(encoding="utf-8") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value

    def _client(self):
        try:
            value = self._json(self.client_path)
            client = value.get("web", value.get("installed", value))
            if not isinstance(client.get("client_id"), str) or not client["client_id"] or len(client["client_id"]) > 1024:
                raise ValueError()
            if client.get("client_secret") is not None and not isinstance(client["client_secret"], str):
                raise ValueError()
            return client
        except (OSError, ValueError, TypeError, AttributeError):
            raise MessageError("Connect this person's Gmail in setup; calendar consent does not grant Gmail access.") from None

    def _token(self):
        try:
            token = self._json(self.token_path)
            if not isinstance(token.get("access_token"), str) or not token["access_token"] or len(token["access_token"]) > 16384:
                raise ValueError()
            scopes = token.get("scopes")
            if not isinstance(scopes, list) or GMAIL_SCOPE not in scopes:
                raise ValueError()
            if token.get("client_id") != self._client()["client_id"]:
                raise ValueError()
            return token
        except (OSError, ValueError, TypeError):
            raise MessageError("This person has not granted Gmail read-only access. Connect Gmail separately in setup.") from None

    def connected(self):
        try:
            self._token()
            return True
        except MessageError:
            return False

    def _request(self, method, url, *, token=None, form=None):
        if not (url == TOKEN_URL or url.startswith(GMAIL_URL + "/")):
            raise MessageError("Unsupported Gmail endpoint.")
        headers = {"Accept": "application/json", "User-Agent": "FlightWall/0.3"}
        data = None
        if token:
            headers["Authorization"] = "Bearer " + token
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = urlencode(form).encode()
        try:
            with self.opener.open(Request(url, data=data, headers=headers, method=method), timeout=20) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except HTTPError as exc:
            # Response payloads and request URLs may contain personal details.
            status = exc.code
            exc.close()
            if status == 401:
                raise _Unauthorized() from None
            if status in {400, 403}:
                raise MessageError("Gmail access was denied. Check this person's consent, Gmail API setup and authorization status.") from None
            raise MessageError("Gmail retrieval is unavailable; summaries may be stale.") from None
        except (URLError, OSError, ValueError, UnicodeError):
            raise MessageError("Gmail retrieval is unavailable; summaries may be stale.") from None

    def _access_token(self, refresh=False):
        client, token = self._client(), self._token()
        try:
            expires = float(token.get("expires_at", 0))
            valid = math.isfinite(expires) and expires > time.time() + 60
        except (ValueError, TypeError):
            valid = False
        if valid and not refresh:
            return token["access_token"]
        if not isinstance(token.get("refresh_token"), str) or not token["refresh_token"]:
            raise MessageError("Gmail offline access has expired. Reconnect this person's Gmail in setup.")
        form = {"client_id": client["client_id"], "refresh_token": token["refresh_token"], "grant_type": "refresh_token"}
        if client.get("client_secret"):
            form["client_secret"] = client["client_secret"]
        try:
            response = self._request("POST", TOKEN_URL, form=form)
        except _Unauthorized:
            raise MessageError("Gmail authorization expired. Reconnect this person's Gmail in setup.") from None
        access = response.get("access_token")
        token_type = response.get("token_type", "Bearer")
        scopes = response.get("scope", " ".join(token["scopes"]))
        scopes = scopes.split() if isinstance(scopes, str) else []
        lifetime = response.get("expires_in", 3600)
        refresh_token = response.get("refresh_token") or token["refresh_token"]
        if not isinstance(access, str) or not access or len(access) > 16384 or GMAIL_SCOPE not in scopes or isinstance(lifetime, bool) or not isinstance(token_type, str) or token_type.casefold() != "bearer" or not isinstance(refresh_token, str) or not refresh_token or len(refresh_token) > 16384:
            raise MessageError("Google did not return valid Gmail read-only authorization. Reconnect Gmail in setup.")
        try:
            lifetime = int(lifetime)
            if not 1 <= lifetime <= 86400:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise MessageError("Google returned an invalid Gmail token expiry. Reconnect Gmail in setup.") from None
        token = {"client_id": client["client_id"], "access_token": access,
                 "refresh_token": refresh_token,
                 "expires_at": time.time() + lifetime, "scopes": scopes}
        private_json(self.token_path, token)
        return access

    def api(self, path, parameters):
        if not re.fullmatch(r"/(?:messages|threads)(?:/[A-Za-z0-9_-]{1,256})?", path) or path in {"/messages/send", "/messages/modify", "/messages/trash", "/messages/delete", "/messages/batchDelete", "/messages/batchModify"}:
            raise MessageError("Unsupported Gmail read operation.")
        url = GMAIL_URL + path + "?" + urlencode(parameters, doseq=True)
        try:
            return self._request("GET", url, token=self._access_token())
        except _Unauthorized:
            try:
                return self._request("GET", url, token=self._access_token(refresh=True))
            except _Unauthorized:
                raise MessageError("Gmail authorization expired. Reconnect this person's Gmail in setup.") from None


class _Unauthorized(Exception):
    pass


class MessageFeeds:
    def __init__(self, data_dir, config_getter):
        self.data_dir = Path(data_dir)
        self.config_getter = config_getter
        self._lock_stores = {profile: PersonalStore(self.account_dir(profile), config_getter) for profile in PROFILES}
        for store in self._lock_stores.values():
            store.lock_path = store.data_dir / "messages.lock"

    def account_dir(self, profile):
        return self.data_dir / "accounts" / _profile(profile)

    def notification_token_path(self, profile):
        return self.account_dir(profile) / "notification-token.txt"

    @contextmanager
    def _locked(self, profile):
        try:
            # Retain the same lock inode while using flock on Batocera and
            # byte-range locking for Windows development. Never omit locking.
            with self._lock_stores[_profile(profile)]._locked():
                yield
        except PersonalError:
            raise MessageError("Message storage is unavailable; check local permissions.") from None

    def rotate_notification_token(self, profile):
        profile = _profile(profile)
        with self._locked(profile):
            token = secrets.token_urlsafe(48)
            path = self.notification_token_path(profile)
            descriptor, temporary = tempfile.mkstemp(prefix=".notification-token-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "w") as stream:
                    if hasattr(os, "fchmod"):
                        os.fchmod(stream.fileno(), 0o600)
                    else:
                        Path(temporary).chmod(0o600)
                    stream.write(token + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return token

    def _notification_apps(self, config, profile):
        apps = {value.strip().casefold() for value in _list(config, profile + "_notification_apps")}
        if any(not value or len(value) > 80 or any(unicodedata.category(char).startswith("C") for char in value) for value in apps):
            raise MessageError("Choose valid notification app identifiers in setup.")
        return apps

    def _eligible_notification_apps(self, config, profile):
        apps = self._notification_apps(config, profile)
        return apps & CHRIS_NOTIFICATION_APPS if profile == "chris" else apps

    def authenticate_notification(self, profile, token):
        try:
            profile = _profile(profile)
            config = self.config_getter()
            if not config.get("messages_enabled", False) or not config.get(profile + "_notifications_enabled", False) or not self._eligible_notification_apps(config, profile):
                return False
            if not isinstance(token, str) or not 32 <= len(token) <= 512:
                return False
            stored = self.notification_token_path(profile).read_text().strip()
            return 32 <= len(stored) <= 512 and hmac.compare_digest(stored.encode(), token.encode())
        except (MessageError, OSError, UnicodeError):
            return False

    def connected(self, profile):
        return GmailClient(self.account_dir(profile)).connected()

    def _empty(self):
        return {"version": 1, "gmail_selection": "", "gmail_last_sync": None, "gmail_error": None, "records": []}

    def _read(self, profile):
        path = self.account_dir(profile) / "messages-cache.json"
        try:
            with path.open(encoding="utf-8") as stream:
                raw = stream.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("records"), list) or len(value["records"]) > MAX_ITEMS:
                raise ValueError()
            return value
        except FileNotFoundError:
            return self._empty()
        except (OSError, ValueError, TypeError):
            raise MessageError("The private summary cache could not be read. Repair it locally; existing data has not been changed.") from None

    def _write(self, profile, value):
        private_json(self.account_dir(profile) / "messages-cache.json", value)

    @staticmethod
    def _safe_item(item, profile, config, now):
        if not isinstance(item, dict) or set(item) != _ITEM_KEYS or item.get("kind") != "message" or item.get("owner") != profile:
            return None
        if not isinstance(item.get("id"), str) or not re.fullmatch(r"[a-f0-9]{32}", item["id"]):
            return None
        if not isinstance(item.get("needs_reply"), bool):
            return None
        if item.get("filtered") is not True or item.get("category") not in ({"rental", "lawyer"} if profile == "chris" else {"personal"}):
            return None
        try:
            observed, expires = _stamp(item["observed_at"]), _stamp(item["expires_at"])
            if observed > now + timedelta(minutes=5) or expires <= now or expires > observed + MAX_AGE or expires <= observed:
                return None
            result = dict(item)
            for key, maximum in (("title", 160), ("body", _preview_limit(config)), ("source", 80), ("reply_label", 40)):
                result[key] = _text(item[key], maximum)
            return result
        except (MessageError, KeyError, TypeError):
            return None

    def status(self, profile):
        profile = _profile(profile)
        config, now = self.config_getter(), _now()
        authorized = self.connected(profile)
        try:
            with self._locked(profile):
                cached = self._read(profile)
                # Expiration also removes old preview text from the private
                # cache while the app is running, including after an opt-out.
                retained = []
                for record in cached["records"]:
                    try:
                        expired = _stamp(record["item"]["expires_at"]) <= now
                    except (MessageError, KeyError, TypeError):
                        expired = False
                    if not expired:
                        retained.append(record)
                if len(retained) != len(cached["records"]):
                    cached["records"] = retained
                    self._write(profile, cached)
            result, selection = [], _selection(config, profile)
            enabled = config.get("messages_enabled", False)
            for record in cached["records"]:
                if not isinstance(record, dict):
                    continue
                channel = record.get("channel")
                if channel == "gmail":
                    allowed = enabled and config.get(profile + "_gmail_enabled", False) and authorized and cached.get("gmail_selection") == selection
                elif channel == "notification":
                    allowed = enabled and config.get(profile + "_notifications_enabled", False) and record.get("app") in self._eligible_notification_apps(config, profile)
                else:
                    allowed = False
                if allowed:
                    item = self._safe_item(record.get("item"), profile, config, now)
                    if item:
                        result.append(item)
            last_sync = cached.get("gmail_last_sync")
            error = _text(cached.get("gmail_error"), 240) or None
            stale = bool(error)
            if config.get(profile + "_gmail_enabled", False):
                stale = stale or not last_sync or now - _stamp(last_sync) > timedelta(minutes=15)
            result.sort(key=lambda item: item["observed_at"], reverse=True)
            return {"items": result[:MAX_ITEMS], "last_sync": last_sync, "error": error, "authorized": authorized, "stale": stale}
        except MessageError as exc:
            return {"items": [], "last_sync": None, "error": str(exc), "authorized": authorized, "stale": True}

    @staticmethod
    def _query(config, profile):
        addresses, domains = _rules(config, profile)
        if profile == "chris":
            domains = sorted(set(domains) | RENTAL_DOMAINS.keys())
        terms = ["from:(" + value + ")" for value in addresses + domains]
        return "in:inbox is:unread newer_than:1d {" + " ".join(terms) + "}" if terms else None

    @staticmethod
    def _message_metadata(message, config, profile, now):
        labels = message.get("labelIds", [])
        if not isinstance(labels, list) or "INBOX" not in labels or "UNREAD" not in labels or "SENT" in labels:
            return None
        headers = (message.get("payload") or {}).get("headers", [])
        senders, subjects = [], []
        for header in headers:
            if not isinstance(header, dict) or not isinstance(header.get("name"), str):
                continue
            if header["name"].casefold() == "from":
                senders.append(header.get("value", ""))
            elif header["name"].casefold() == "subject":
                subjects.append(header.get("value", ""))
        if len(senders) != 1 or not isinstance(senders[0], str) or len(senders[0]) > 2048:
            return None
        parsed = getaddresses(senders)
        if len(parsed) != 1:
            return None
        try:
            sender = _address(parsed[0][1])
        except MessageError:
            return None
        category = _category(sender, config, profile)
        if category is None:
            return None
        try:
            received = datetime.fromtimestamp(int(message.get("internalDate", "")) / 1000, UTC)
        except (ValueError, TypeError, OverflowError, OSError):
            return None
        if not now - MAX_AGE <= received <= now + timedelta(minutes=5):
            return None
        subject = subjects[0] if subjects and isinstance(subjects[0], str) else ""
        try:
            subject = "".join(part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part for part, charset in decode_header(subject[:8192]))
        except (ValueError, LookupError):
            subject = ""
        identifier, thread = message.get("id"), message.get("threadId")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier) or not isinstance(thread, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", thread):
            return None
        item = {"id": hashlib.sha256((profile + ":gmail:" + identifier).encode()).hexdigest()[:32],
                "kind": "message", "owner": profile, "title": _text(subject, 160) or "Message from " + category,
                "body": _text(message.get("snippet", ""), _preview_limit(config)), "source": "Gmail / " + category,
                "category": ("rental" if category in {"Airbnb", "VRBO"} else "lawyer") if profile == "chris" else "personal", "filtered": True,
                "observed_at": _iso(received), "expires_at": _iso(received + MAX_AGE),
                "needs_reply": False, "reply_label": "Reply not inferred"}
        return thread, received, item

    def _gmail_items(self, profile, config):
        query = self._query(config, profile)
        if not query:
            return []
        client, now = GmailClient(self.account_dir(profile)), _now()
        parameters = {"q": query, "maxResults": 20, "fields": "messages(id),nextPageToken"}
        candidates, seen, inspected = {}, set(), 0
        for _page in range(2):
            response = client.api("/messages", parameters)
            messages = response.get("messages", [])
            if not isinstance(messages, list):
                raise MessageError("Gmail returned an invalid selected-message list.")
            for summary in messages[:20]:
                identifier = summary.get("id") if isinstance(summary, dict) else None
                if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier) or identifier in seen:
                    continue
                seen.add(identifier)
                inspected += 1
                message = client.api("/messages/" + quote(identifier, safe=""), {
                    "format": "metadata", "metadataHeaders": ["From", "Subject"],
                    "fields": "id,threadId,internalDate,labelIds,payload(headers)",
                })
                parsed = self._message_metadata(message, config, profile, now)
                if parsed:
                    thread, received, item = parsed
                    # METADATA is not documented to include snippets. A partial
                    # FULL response requests only the approved preview field;
                    # payload/body/attachments never cross the network here.
                    snippet = client.api("/messages/" + quote(identifier, safe=""), {"format": "full", "fields": "snippet"})
                    item["body"] = _text(snippet.get("snippet", ""), _preview_limit(config))
                    if thread not in candidates or received > candidates[thread][0]:
                        candidates[thread] = (received, item)
                if len(candidates) >= MAX_ITEMS or inspected >= 40:
                    break
            token = response.get("nextPageToken")
            if len(candidates) >= MAX_ITEMS or inspected >= 40 or not isinstance(token, str) or not token or len(token) > 2048:
                break
            parameters["pageToken"] = token
        results = []
        for thread, (received, item) in sorted(candidates.items(), key=lambda pair: pair[1][0], reverse=True)[:MAX_ITEMS]:
            # Read timestamps/labels only, never other messages' bodies or headers.
            metadata = client.api("/threads/" + quote(thread, safe=""), {"format": "metadata", "fields": "messages(id,internalDate,labelIds)"})
            latest_sent, latest_inbound = None, received
            for message in metadata.get("messages", [])[:2000]:
                if not isinstance(message, dict) or not isinstance(message.get("labelIds"), list):
                    continue
                try:
                    stamp = datetime.fromtimestamp(int(message.get("internalDate", "")) / 1000, UTC)
                except (ValueError, TypeError, OverflowError, OSError):
                    continue
                if "SENT" in message["labelIds"]:
                    latest_sent = max(latest_sent, stamp) if latest_sent else stamp
                else:
                    latest_inbound = max(latest_inbound, stamp)
            item["needs_reply"] = latest_sent is None or latest_sent < latest_inbound
            item["reply_label"] = "May need reply" if item["needs_reply"] else "Reply not inferred"
            results.append({"channel": "gmail", "item": item})
        return results

    def sync(self, profile):
        profile = _profile(profile)
        config = self.config_getter()
        if not config.get("messages_enabled", False) or not config.get(profile + "_gmail_enabled", False):
            return self.status(profile)
        selection = _selection(config, profile)
        try:
            records = self._gmail_items(profile, config)
            if _selection(self.config_getter(), profile) != selection:
                return self.status(profile)
            with self._locked(profile):
                cached = self._read(profile)
                notifications = [record for record in cached["records"] if isinstance(record, dict) and record.get("channel") == "notification"
                                 and self._safe_item(record.get("item"), profile, config, _now())]
                cached.update(gmail_selection=selection, gmail_last_sync=_iso(_now()), gmail_error=None,
                              records=sorted(records + notifications, key=lambda record: record["item"]["observed_at"], reverse=True)[:MAX_ITEMS])
                self._write(profile, cached)
        except (MessageError, OSError, ValueError, TypeError, AttributeError, KeyError):
            try:
                with self._locked(profile):
                    cached = self._read(profile)
                    cached["gmail_error"] = "Gmail retrieval failed; selected summaries may be stale. Check this person's Gmail connection in setup."
                    self._write(profile, cached)
            except (MessageError, OSError):
                pass
        return self.status(profile)

    def receive_notification(self, profile, payload):
        profile = _profile(profile)
        config, now = self.config_getter(), _now()
        if not config.get("messages_enabled", False) or not config.get(profile + "_notifications_enabled", False):
            raise MessageError("This person has not enabled selected phone notifications in setup.")
        allowed = {"app", "sender", "preview", "timestamp", "request_id"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise MessageError("Use only app, sender, preview, timestamp and optional request_id.")
        app = payload.get("app")
        if not isinstance(app, str) or not 1 <= len(app.strip()) <= 80 or app.strip().casefold() not in self._notification_apps(config, profile):
            raise MessageError("This notification app has not been selected by this person in setup.")
        if profile == "chris" and app.strip().casefold() not in CHRIS_NOTIFICATION_APPS:
            raise MessageError("Chris's notification bridge currently accepts only Airbnb, VRBO or HomeAway. Other phone apps are not enabled for this profile.")
        sender, preview = payload.get("sender"), payload.get("preview")
        if not isinstance(sender, str) or not 1 <= len(sender) <= 160 or not isinstance(preview, str) or not 1 <= len(preview) <= 4096:
            raise MessageError("Provide a short sender name and preview, without a full message or attachments.")
        sender, preview = _text(sender, 80), _text(preview, _preview_limit(config))
        if not sender or not preview:
            raise MessageError("The selected notification needs a readable sender and preview.")
        observed = _stamp(payload.get("timestamp"))
        if observed <= now - MAX_AGE or observed > now + timedelta(minutes=5):
            raise MessageError("Send a recent notification timestamp, within the last 24 hours.")
        request_id = payload.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 512 or any(unicodedata.category(c).startswith("C") for c in request_id)):
            raise MessageError("Use a request_id of 1 to 512 ordinary characters.")
        normalized = {"app": app.strip().casefold(), "sender": sender, "preview": preview, "timestamp": _iso(observed)}
        fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        request_hash = hashlib.sha256((profile + ":" + (request_id or fingerprint)).encode()).hexdigest()
        item = {"id": request_hash[:32], "kind": "message", "owner": profile, "title": sender,
                "category": "rental" if profile == "chris" else "personal", "filtered": True,
                "body": preview, "source": _text(app, 80), "observed_at": _iso(observed), "expires_at": _iso(observed + MAX_AGE),
                "needs_reply": False, "reply_label": "Reply not inferred"}
        with self._locked(profile):
            cached = self._read(profile)
            records = [record for record in cached["records"] if isinstance(record, dict) and self._safe_item(record.get("item"), profile, config, now)]
            for record in records:
                if record.get("request_hash") == request_hash:
                    if record.get("payload_hash") != fingerprint:
                        raise MessageError("This notification request_id was already used for different details.")
                    return {"received": True, "duplicate": True, "id": item["id"], "profile": profile}
            records.append({"channel": "notification", "app": normalized["app"], "request_hash": request_hash,
                            "payload_hash": fingerprint, "item": item})
            cached["records"] = sorted(records, key=lambda record: record["item"]["observed_at"], reverse=True)[:MAX_ITEMS]
            self._write(profile, cached)
        return {"received": True, "duplicate": False, "id": item["id"], "profile": profile}
