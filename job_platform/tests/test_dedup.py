from core import db
from core.dedup import process_record
from core.models import JobRecord


def make_record(**overrides) -> JobRecord:
    base = dict(
        job_id="greenhouse:acme:1",
        company="Acme",
        title="ML Engineer",
        location="Hyderabad, India",
        source_platform="greenhouse",
        raw_description="Build RAG pipelines.",
    )
    base.update(overrides)
    return JobRecord(**base)


def test_new_then_unchanged_then_changed(tmp_path):
    conn = db.connect(tmp_path / "t.db")

    first = process_record(conn, make_record())
    assert first.status == "new" and first.needs_scoring

    again = process_record(conn, make_record())
    assert again.status == "unchanged" and not again.needs_scoring

    edited = process_record(conn, make_record(raw_description="Build agents."))
    assert edited.status == "changed" and edited.needs_scoring


def test_cross_platform_repost_detected(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    process_record(conn, make_record())
    # Same company+title+location on a different platform -> duplicate, never scored.
    repost = process_record(
        conn,
        make_record(job_id="lever:acme:xyz", source_platform="lever"),
    )
    assert repost.status == "duplicate" and not repost.needs_scoring
    row = db.get_job(conn, "lever:acme:xyz")
    assert row["duplicate_of"] == "greenhouse:acme:1"


def test_unchanged_with_cached_eval_never_rescored(tmp_path):
    from core.models import Evaluation

    conn = db.connect(tmp_path / "t.db")
    rec = make_record()
    process_record(conn, rec)
    db.save_evaluation(
        conn,
        Evaluation(
            job_id=rec.job_id, content_hash=rec.content_hash,
            pass1_requirements={}, pass2_verification={},
            relevance_score=80, skill_gap="none", verdict="APPLY", reasoning="fit",
        ),
    )
    # Re-seen identical record: no scoring needed (cache keyed by content_hash).
    assert not process_record(conn, make_record()).needs_scoring
    # Changed content invalidates the cache.
    assert process_record(conn, make_record(raw_description="different")).needs_scoring


def test_application_status_rows_are_append_only(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.ensure_application_row(conn, "j1")
    conn.execute(
        "UPDATE application_status SET status='Applied', notes='manual note' WHERE job_id='j1'"
    )
    conn.commit()
    db.ensure_application_row(conn, "j1")  # must not clobber manual edits
    row = conn.execute("SELECT * FROM application_status WHERE job_id='j1'").fetchone()
    assert row["status"] == "Applied" and row["notes"] == "manual note"
    db.record_tailoring_outputs(conn, "j1", "/out/r.tex", None)
    row = conn.execute("SELECT * FROM application_status WHERE job_id='j1'").fetchone()
    assert row["status"] == "Applied" and row["resume_path"] == "/out/r.tex"


def test_scheduler_backoff(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.source_due(conn, "greenhouse", 2)
    db.record_failure(conn, "greenhouse", base_minutes=30)
    assert not db.source_due(conn, "greenhouse", 2)  # backing off
    db.record_success(conn, "greenhouse")
    assert not db.source_due(conn, "greenhouse", 2)  # just succeeded
    assert db.source_due(conn, "greenhouse", 0)      # zero interval -> due again
