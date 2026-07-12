from datetime import date

from connectors.greenhouse import GreenhouseConnector
from core.models import RawFetch

SAMPLE = {
    "jobs": [
        {
            "id": 4567,
            "title": "Machine Learning Engineer",
            "updated_at": "2026-07-01T10:00:00-04:00",
            "first_published": "2026-06-28T09:00:00-04:00",
            "location": {"name": "Bengaluru, India"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/4567",
            "content": "<p>Build <b>LLM</b> pipelines &amp; RAG systems.</p>",
            "departments": [{"name": "AI Platform"}],
        },
        {   # malformed job: missing title -> skipped, not crashed
            "id": 999,
            "title": "",
            "location": None,
        },
    ]
}


def make_raw() -> RawFetch:
    return RawFetch(
        source_platform="greenhouse",
        company="Acme",
        company_config={"company": "Acme", "slug": "acme"},
        url="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
        payload=SAMPLE,
    )


def test_normalize_maps_unified_schema():
    records = GreenhouseConnector().normalize(make_raw())
    assert len(records) == 1
    rec = records[0]
    assert rec.job_id == "greenhouse:acme:4567"
    assert rec.company == "Acme"
    assert rec.title == "Machine Learning Engineer"
    assert rec.location == "Bengaluru, India"
    assert rec.department == "AI Platform"
    assert rec.posted_date == date(2026, 6, 28)
    assert "LLM" in rec.raw_description and "<p>" not in rec.raw_description
    assert rec.content_hash and rec.dedup_hash


def test_malformed_job_is_skipped_not_fatal():
    records = GreenhouseConnector().normalize(make_raw())
    assert all(r.title for r in records)


def test_registry_discovery():
    from connectors import discover

    registry = discover()
    for expected in (
        "greenhouse", "lever", "ashby", "workday",
        "smartrecruiters", "careers_direct", "wellfound", "internshala",
    ):
        assert expected in registry
