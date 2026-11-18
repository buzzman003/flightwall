"""Google's partial-field empty-list response must not break travel imports."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener
from unittest.mock import patch

import pytest

from flightwall.config import private_json
from flightwall.delta_travel import DeltaTravel
from flightwall.messages import GMAIL_SCOPE, MessageError


@pytest.mark.parametrize("person", ["chris", "lisa"])
def test_zero_delta_matches_are_healthy_over_real_http_without_fetches_or_mailbox_writes(tmp_path, person):
    calls = []

    class GoogleEmptyList(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            fields = parse_qs(parsed.query).get("fields", [None])[0]
            calls.append(("GET", parsed.path, fields))
            if parsed.path != "/messages":
                self.send_error(404)
            elif fields == "messages(id),nextPageToken":
                self.send_response(204)
                self.end_headers()
            elif fields == "messages(id),nextPageToken,resultSizeEstimate":
                raw = b'{"resultSizeEstimate":0}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.send_error(400)

        def do_POST(self):
            calls.append(("POST", urlsplit(self.path).path, None))
            self.send_error(405)

        do_PUT = do_DELETE = do_POST

    config = {"demo": False, "delta_travel_enabled": person == "chris",
              "delta_travel_lisa_mail_enabled": person == "lisa",
              "delta_travel_owner_names": '["ALEX MORGAN"]',
              "delta_travel_lisa_names": '["CASEY TAYLOR"]'}
    feed = DeltaTravel(tmp_path, lambda: config, person=person)
    account = tmp_path / "accounts" / person
    private_json(account / "gmail-client.json", {"installed": {"client_id": "synthetic-client"}})
    private_json(account / "gmail-token.json", {"client_id": "synthetic-client", "access_token": "synthetic-access",
                 "refresh_token": "synthetic-refresh", "expires_at": time.time() + 3600, "scopes": [GMAIL_SCOPE]})
    credentials = {path: path.read_bytes() for path in account.iterdir()}
    feed.client.opener = build_opener(ProxyHandler({}))
    server = ThreadingHTTPServer(("127.0.0.1", 0), GoogleEmptyList)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    now = datetime.now(timezone.utc)
    try:
        with patch("flightwall.messages.GMAIL_URL", f"http://127.0.0.1:{server.server_port}"):
            # The old field selection reproduces the real failure rather than
            # relaxing the shared Gmail client's JSON/metadata requirements.
            with pytest.raises(MessageError, match="unavailable"):
                feed.client.api("/messages", {"fields": "messages(id),nextPageToken"})
            result = feed.sync(now)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert result == {"enabled": True, "status": "ready", "last_sync": now.isoformat(), "error": None,
                      "inspected": 0, "imported": 0, "review_needed": 0}
    assert feed.status() == result
    assert json.loads(feed.path.read_text())["status"] == "ready"
    assert feed.travel.list_records() == []
    assert not feed.travel.storage.path.exists()
    assert all(path.read_bytes() == original for path, original in credentials.items())
    assert calls == [("GET", "/messages", "messages(id),nextPageToken"),
                     ("GET", "/messages", "messages(id),nextPageToken,resultSizeEstimate")]
