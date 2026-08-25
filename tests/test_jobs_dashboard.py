"""The Jobs page and the service functions behind it (spec sections 30-35)."""

import pytest

from paper_automation import service
from paper_automation.models import Phase, ProcessingState
from paper_automation.state import StateStore

MONTH = "August 2026"


def _make_job(cfg, employee, client, phase, finish_state=None):
    with StateStore(cfg.state_db) as store:
        job_id = store.enqueue_job(MONTH, employee, client, phase)
        store.start_job(job_id)
        if finish_state is not None:
            store.finish_job(job_id, finish_state, "")
    return job_id


# --- service.job_overview -----------------------------------------------


def test_job_overview_is_empty_before_any_job_exists(cfg):
    data = service.job_overview(cfg)
    assert data["jobs"] == []
    assert data["total"] == 0


def test_job_overview_lists_recorded_jobs(cfg):
    _make_job(cfg, "Priya", "Acme", Phase.REVIEW, ProcessingState.REVIEW_COMPLETED)
    data = service.job_overview(cfg)
    assert len(data["jobs"]) == 1
    assert data["jobs"][0]["employee"] == "Priya"
    assert data["jobs"][0]["client"] == "Acme"


def test_job_overview_shows_newest_first(cfg):
    _make_job(cfg, "A", "One", Phase.REVIEW, ProcessingState.REVIEW_COMPLETED)
    _make_job(cfg, "B", "Two", Phase.REVIEW, ProcessingState.REVIEW_COMPLETED)
    data = service.job_overview(cfg)
    assert [j["client"] for j in data["jobs"]] == ["Two", "One"]


def test_job_overview_counts_by_status(cfg):
    _make_job(cfg, "A", "One", Phase.REVIEW, ProcessingState.REVIEW_COMPLETED)
    _make_job(cfg, "B", "Two", Phase.REVIEW, ProcessingState.FAILED)
    data = service.job_overview(cfg)
    assert data["counts"]["REVIEW_COMPLETED"] == 1
    assert data["counts"]["FAILED"] == 1
    assert data["total"] == 2


def test_job_overview_can_filter_by_status(cfg):
    _make_job(cfg, "A", "One", Phase.REVIEW, ProcessingState.REVIEW_COMPLETED)
    _make_job(cfg, "B", "Two", Phase.REVIEW, ProcessingState.FAILED)
    data = service.job_overview(cfg, status="FAILED")
    assert [j["client"] for j in data["jobs"]] == ["Two"]
    assert data["filter"] == "FAILED"


def test_job_overview_labels_are_plain_english(cfg):
    _make_job(cfg, "A", "One", Phase.REVIEW, ProcessingState.FAILED)
    job = service.job_overview(cfg)["jobs"][0]
    assert job["status_label"] == "Failed"
    assert job["phase_label"] == "Checking for mistakes"
    assert job["provider"] == "Codex"


def test_job_overview_still_works_with_no_state_db(tmp_path):
    from paper_automation.config import Config

    cfg = Config(research_papers_root=tmp_path, state_db=tmp_path / "nope.sqlite3")
    data = service.job_overview(cfg)
    assert data["jobs"] == []
    assert data["counts"] == {}


# --- service.system_health ------------------------------------------------


def test_system_health_reports_storage_state(cfg, storage):
    cfg.research_papers_root.mkdir(parents=True, exist_ok=True)
    health = service.system_health(cfg, storage, {"running": False})
    assert health["storage_ok"] is True


def test_system_health_reports_missing_storage(cfg, storage):
    health = service.system_health(cfg, storage, {"running": False})
    assert health["storage_ok"] is False


def test_system_health_flags_lan_exposure(cfg, storage):
    cfg.web_host = "0.0.0.0"
    assert service.system_health(cfg, storage, {})["lan_reachable"] is True
    cfg.web_host = "127.0.0.1"
    assert service.system_health(cfg, storage, {})["lan_reachable"] is False


def test_system_health_reports_concurrency_and_task_mode(cfg, storage):
    cfg.max_concurrent_jobs = 3
    health = service.system_health(cfg, storage, {})
    assert health["max_concurrent_jobs"] == 3
    assert health["task_mode"] == cfg.task_mode


# --- /jobs route -----------------------------------------------------------


@pytest.fixture
def client(cfg, monkeypatch):
    from paper_automation import config as config_module
    from webui import app as webapp

    monkeypatch.setattr(webapp, "current_config", lambda: cfg)
    monkeypatch.setattr(config_module, "load", lambda path=None: cfg)
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


def test_jobs_page_renders(client):
    res = client.get("/jobs")
    assert res.status_code == 200
    assert b"Jobs" in res.data
    assert b"System status" in res.data


def test_jobs_page_lists_a_job(client, cfg):
    _make_job(cfg, "Priya", "Acme", Phase.REVIEW, ProcessingState.REVIEW_COMPLETED)
    res = client.get("/jobs")
    assert b"Priya" in res.data
    assert b"Acme" in res.data


def test_jobs_page_filter_link_works(client, cfg):
    _make_job(cfg, "A", "One", Phase.REVIEW, ProcessingState.REVIEW_COMPLETED)
    _make_job(cfg, "B", "Two", Phase.REVIEW, ProcessingState.FAILED)
    res = client.get("/jobs?status=FAILED")
    assert b"Two" in res.data
    assert b"One" not in res.data
