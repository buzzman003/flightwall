from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from flightwall.agenda_google_tasks import AgendaGoogleTasks, TasksClient


def test_lists_only_open_items_and_only_requests_title_status_and_due(tmp_path, monkeypatch):
    client = TasksClient(tmp_path)
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
            return {"items": [{"status": "needsAction", "title": "Earlier", "due": "2026-01-01T00:00:00Z"}]}
        return {"nextPageToken": "page-2", "items": [
            {"status": "completed", "title": "Finished"}, {"status": "needsAction", "hidden": True, "title": "Hidden"},
            {"status": "needsAction", "deleted": True, "title": "Deleted"}, {"status": "needsAction", "title": "Undated"}]}

    monkeypatch.setattr(client, "_get", get)
    assert client.unfinished() == ["Earlier", "Undated"] and len(requests) == 3


def test_opt_in_expiry_and_disconnect(tmp_path, monkeypatch):
    config = {"demo": False, "chris_agenda_enabled": True}
    feed = AgendaGoogleTasks(tmp_path, lambda: config)
    client = feed.client
    client.account_dir.mkdir(parents=True)
    for path in (client.client_path, client.token_path):
        path.touch()
    (client.account_dir / "google-token.json").write_text('{"refresh_token":"synthetic-bound-grant"}')
    monkeypatch.setattr(client, "connected", lambda: True)
    monkeypatch.setattr(client, "_access_token", lambda: "synthetic")
    monkeypatch.setattr(client, "unfinished", lambda: ["Task"])
    now = datetime.now(timezone.utc)
    feed.refresh(now)
    assert feed.titles(now) == ["Task"]
    assert feed.titles(now + timedelta(minutes=30)) == []
    config["chris_agenda_enabled"] = False
    assert feed.titles(now) == []
    config["chris_agenda_enabled"] = True
    client.token_path.unlink()
    assert feed.titles(now) == []
