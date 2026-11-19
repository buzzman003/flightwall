from datetime import date, datetime, timedelta, timezone
import hashlib
from urllib.parse import parse_qs, urlsplit

from flightwall.agenda_google_tasks import AgendaGoogleTasks, TasksClient


def test_openid_is_optional_and_cannot_replace_either_required_permission():
    assert TasksClient.granted_scopes(list(TasksClient.scopes))
    assert TasksClient.granted_scopes([*TasksClient.scopes, "openid"])
    for scopes in (["openid", "email"], ["openid", TasksClient.scope],
                   [*TasksClient.scopes, "openid", "profile"],
                   [*TasksClient.scopes, "https://www.googleapis.com/auth/tasks"]):
        assert not TasksClient.granted_scopes(scopes)


def test_lists_only_open_items_and_only_requests_title_status_and_due(tmp_path, monkeypatch):
    client = TasksClient(tmp_path)
    monkeypatch.setattr(client, "calendar_binding", lambda: "a" * 64)
    requests = []

    def get(url):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        requests.append(parsed.path)
        assert parsed.netloc == "tasks.googleapis.com" and "notes" not in query["fields"][0]
        if parsed.path.endswith("@me/lists"):
            return {"items": [{"id": "test/A"}]}
        assert parsed.path.endswith("test%2FA/tasks") and query["showCompleted"] == ["false"]
        if "pageToken" in query:
            return {"items": [{"id": "earlier", "status": "needsAction", "title": "Earlier", "due": "2026-01-01T00:00:00Z"}]}
        return {"nextPageToken": "page-2", "items": [
            {"status": "completed", "title": "Finished"}, {"status": "needsAction", "hidden": True, "title": "Hidden"},
            {"status": "needsAction", "deleted": True, "title": "Deleted"},
            {"id": "no-date", "status": "needsAction", "title": "Undated"}]}

    monkeypatch.setattr(client, "_get", get)
    found = client.unfinished()
    assert set(found.values()) == {(date(2026, 1, 1), "Earlier"), (None, "Undated")}
    assert len(requests) == 3 and all(len(key) == 64 for key in found)
    monkeypatch.setattr(client, "calendar_binding", lambda: "b" * 64)
    assert found.keys().isdisjoint(client.unfinished().keys())


def make_feed(tmp_path, monkeypatch, rows, config):
    feed = AgendaGoogleTasks(tmp_path, lambda: config)
    client = feed.client
    client.account_dir.mkdir(parents=True, exist_ok=True)
    for path in (client.client_path, client.token_path):
        path.touch()
    (client.account_dir / "google-token.json").write_text('{"refresh_token":"synthetic-bound-grant"}')
    monkeypatch.setattr(client, "connected", lambda: True)
    monkeypatch.setattr(client, "_access_token", lambda: "synthetic")
    monkeypatch.setattr(client, "unfinished", lambda: rows.copy())
    return feed


KEY = hashlib.sha256(b"synthetic task").hexdigest()


def test_opt_in_expiry_and_disconnect(tmp_path, monkeypatch):
    config = {"demo": False, "chris_agenda_enabled": True}
    feed = make_feed(tmp_path, monkeypatch, {KEY: (None, "Task")}, config)
    now = datetime.now(timezone.utc)
    feed.refresh(now)
    assert [item["title"] for item in feed.visible_tasks(now)] == ["Task"]
    assert feed.visible_tasks(now + timedelta(minutes=30)) == []
    config["chris_agenda_enabled"] = False
    assert feed.visible_tasks(now) == []
    config["chris_agenda_enabled"] = True
    feed.client.token_path.unlink()
    assert feed.visible_tasks(now) == []


def test_undated_clock_only_starts_for_rendered_rows_survives_restart_and_doesnt_restart(tmp_path, monkeypatch):
    config = {"demo": False, "chris_agenda_enabled": True, "timezone": "America/Los_Angeles"}
    other = "b" * 64
    rows = {KEY: (None, "Private title"), other: (None, "Unseen page")}
    feed = make_feed(tmp_path, monkeypatch, rows, config)
    first = datetime(2026, 10, 31, 6, 59, tzinfo=timezone.utc)  # Fri Oct 30 23:59 PDT
    feed.refresh(first - timedelta(days=7))
    feed.visible_tasks(first - timedelta(days=7))
    assert not feed._history.path.exists()
    feed.refresh(first)
    assert {r["display_id"] for r in feed.visible_tasks(first)} == set(rows)
    feed.mark_displayed([KEY, "c" * 64], first)
    text = feed._history.path.read_text()
    assert KEY in text and other not in text and "c" * 64 not in text
    assert "Private title" not in text and "synthetic-bound-grant" not in text
    cutoff = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)  # Monday midnight PST after DST
    fresh = make_feed(tmp_path, monkeypatch, rows, config)
    fresh.refresh(cutoff - timedelta(seconds=1))
    still_open = fresh.visible_tasks(cutoff - timedelta(seconds=1))
    prior = next(r for r in still_open if r["title"] == "Private title")
    assert prior == {"title": "Private title", "display_until": cutoff.isoformat()}
    fresh.mark_displayed([KEY], cutoff)
    assert [r["title"] for r in fresh.visible_tasks(cutoff)] == ["Unseen page"]
    later = cutoff + timedelta(days=50)
    fresh.refresh(later)
    assert [r["display_id"] for r in fresh.visible_tasks(later)] == [other]


def test_due_window_tracks_three_local_dates_across_midnight_and_filters_before_limit(tmp_path, monkeypatch):
    config = {"demo": False, "chris_agenda_enabled": True, "timezone": "America/Chicago"}
    rows = {f"{n:064x}": (date(2026, 11, 10), f"Past {n}") for n in range(45)}
    rows.update({f"{100+n:064x}": (date(2026, 11, n), f"Due {n}") for n in range(17, 22)})
    feed = make_feed(tmp_path, monkeypatch, rows, config)
    now = datetime(2026, 11, 19, 5, 59, tzinfo=timezone.utc)  # still November 18
    feed.refresh(now)
    visible = feed.visible_tasks(now)
    assert [r["title"] for r in visible] == ["Due 18", "Due 19", "Due 20"]
    assert visible[0]["display_until"] == (now + timedelta(minutes=1)).isoformat()
    assert all("display_id" not in r for r in visible)
    assert [r["title"] for r in feed.visible_tasks(now + timedelta(minutes=1))] == ["Due 19", "Due 20", "Due 21"]
    assert not feed._history.path.exists()


def test_damaged_history_cannot_restart_undated_timers(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    feed = make_feed(tmp_path, monkeypatch, {KEY: (None, "Undated"), "a"*64: (now.date(), "Dated")},
                     {"demo": False, "chris_agenda_enabled": True})
    feed._history.path.write_text("incomplete")
    feed.refresh(now)
    assert [r["title"] for r in feed.visible_tasks(now)] == ["Dated"]


def test_full_history_preserves_expired_rows_and_admits_only_remaining_slots(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr("flightwall.agenda_google_tasks.MAX_DISPLAY_HISTORY", 2)
    now = datetime.now(timezone.utc)
    rows = {KEY: (None, "Expired"), "b"*64: (None, "One"), "c"*64: (None, "Two")}
    feed = make_feed(tmp_path, monkeypatch, rows, {"demo": False, "chris_agenda_enabled": True})
    feed._history.path.write_text(json.dumps({KEY: "2020-01-01"}))
    feed.refresh(now)
    assert [r["title"] for r in feed.visible_tasks(now)] == ["One"]
    feed.mark_displayed(["b"*64, "c"*64], now)
    assert set(json.loads(feed._history.path.read_text())) == {KEY, "b"*64}
    assert [r["title"] for r in feed.visible_tasks(now)] == ["One"]
