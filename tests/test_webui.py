"""Web routes. Thin layer, so these check wiring and the safety boundaries."""

import pytest

flask = pytest.importorskip("flask", reason="web UI is optional")


@pytest.fixture
def client(cfg, tmp_path, monkeypatch):
    """A test client whose config points at the temporary tree."""
    from paper_automation import config as config_module
    from webui import app as webapp

    monkeypatch.setattr(webapp, "current_config", lambda: cfg)
    monkeypatch.setattr(config_module, "load", lambda path=None: cfg)
    webapp.app.config["TESTING"] = True
    webapp.runs = __import__(
        "paper_automation.service", fromlist=["service"]
    ).RunManager()
    return webapp.app.test_client()


def test_prompt_editor_renders_both_phases(client):
    res = client.get("/prompts")
    assert res.status_code == 200
    assert b"Finding the mistakes" in res.data
    assert b"Writing the corrections" in res.data


def test_prompt_editor_save_and_restore(client, cfg):
    res = client.post("/prompts", data={
        "phase": "review", "action": "save", "body": "Custom {client} instructions.",
    })
    assert res.status_code == 200
    assert b"Saved as version 1" in res.data

    res = client.get("/prompts")
    assert b"Custom {client} instructions." in res.data
    assert b"edited" in res.data

    res = client.post("/prompts", data={"phase": "review", "action": "reset"})
    assert b"built-in instructions again" in res.data


def test_prompt_editor_rejects_an_unknown_placeholder(client):
    res = client.post("/prompts", data={
        "phase": "review", "action": "save", "body": "Bad {nonsense}",
    })
    assert b"Unknown placeholder" in res.data


def test_create_folder_api_creates_an_employee_folder(client, cfg):
    res = client.post("/api/create-folder", json={
        "month": "August 2026", "employee": "Priya",
    })
    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert (cfg.research_papers_root / "August 2026" / "Priya").is_dir()


def test_create_folder_api_creates_a_client_folder(client, cfg):
    res = client.post("/api/create-folder", json={
        "month": "August 2026", "employee": "Priya", "client": "Acme",
    })
    assert res.status_code == 200
    assert (cfg.research_papers_root / "August 2026" / "Priya" / "Acme").is_dir()


def test_create_folder_api_requires_month_and_employee(client):
    res = client.post("/api/create-folder", json={"employee": "Priya"})
    assert res.status_code == 400
    res = client.post("/api/create-folder", json={"month": "August 2026"})
    assert res.status_code == 400


def test_create_folder_api_rejects_a_bad_name(client):
    res = client.post("/api/create-folder", json={
        "month": "August 2026", "employee": "../evil",
    })
    assert res.status_code == 400
    assert res.get_json()["ok"] is False


def test_dashboard_renders(client, make_client):
    make_client("Vani.docx")
    res = client.get("/")
    assert res.status_code == 200
    assert b"Vani" in res.data


def test_dashboard_shows_the_pending_action(client, make_client):
    make_client("Vani.docx")
    assert b"Waiting to be checked" in client.get("/").data


def test_dashboard_survives_a_missing_papers_folder(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"find your papers folder" in res.data.lower()


def test_history_and_settings_render(client):
    assert client.get("/history").status_code == 200
    assert client.get("/settings").status_code == 200


def test_scan_api_returns_rows(client, make_client):
    make_client("Vani.docx")
    data = client.get("/api/scan").get_json()
    assert data["rows"][0]["client"] == "Vani"
    assert data["rows"][0]["status"] == "READY_REVIEW"


def test_preview_api_changes_nothing(client, make_client):
    folder = make_client("Vani.docx")
    before = sorted(p.name for p in folder.iterdir())
    assert client.get("/api/preview").status_code == 200
    assert sorted(p.name for p in folder.iterdir()) == before


def test_status_api_is_idle_initially(client):
    assert client.get("/api/status").get_json()["running"] is False


def test_open_api_rejects_a_path_outside_the_tree(client, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    res = client.post("/api/open", json={"path": str(outside)})
    assert res.get_json()["ok"] is False


def test_open_api_requires_a_path(client):
    assert client.post("/api/open", json={}).status_code == 400


def test_second_run_request_is_refused(client, make_client, cfg):
    """The UI must not be able to start two pipelines over the same folders."""
    from webui import app as webapp

    make_client("Vani.docx")
    webapp.runs._running = True
    res = client.post("/api/run", json={"phase": "both"})
    assert res.status_code == 409
    assert res.get_json()["ok"] is False
    webapp.runs._running = False
