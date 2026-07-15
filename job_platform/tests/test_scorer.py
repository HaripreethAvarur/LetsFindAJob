import pytest

from core import db
from core.llm import LLMError, _extract_json
from core.models import JobRecord
from core.scorer import PASS1_SCHEMA, PASS2_SCHEMA, Scorer


class FakeProvider:
    """Deterministic provider: returns queued responses per call."""

    name = "fake"

    def __init__(self, pass1, pass2):
        self.responses = [pass1, pass2]
        self.calls = 0

    def complete_json(self, system, user, schema):
        self.calls += 1
        return self.responses.pop(0)

    def complete_text(self, system, user, max_tokens=16000):
        return ""


def make_rec(desc="Need LangChain and RAG experience.") -> JobRecord:
    return JobRecord(
        job_id="greenhouse:acme:1", company="Acme", title="ML Engineer",
        location="Remote, India", source_platform="greenhouse", raw_description=desc,
    )


PASS1 = {"hard_requirements": ["LangChain", "2+ years ML"], "soft_requirements": ["Kubernetes"]}


def pass2(statuses, score, verdict):
    return {
        "checks": [
            {"requirement": r, "status": s, "evidence": "profile"}
            for r, s in zip(PASS1["hard_requirements"], statuses, strict=True)
        ],
        "relevance_score": score,
        "skill_gap": "none",
        "verdict": verdict,
        "reasoning": "fit",
    }


def make_scorer(provider):
    return Scorer(provider, profile_summary="profile text", years_experience=2, apply_threshold=75)


def test_apply_when_all_hard_met_and_score_high():
    scorer = make_scorer(FakeProvider(PASS1, pass2(["met", "partial"], 85, "APPLY")))
    ev = scorer.score(make_rec())
    assert ev.verdict == "APPLY" and ev.relevance_score == 85


def test_apply_downgraded_when_hard_requirement_missing():
    scorer = make_scorer(FakeProvider(PASS1, pass2(["met", "missing"], 90, "APPLY")))
    assert scorer.score(make_rec()).verdict == "MAYBE"


def test_apply_downgraded_when_score_below_threshold():
    scorer = make_scorer(FakeProvider(PASS1, pass2(["met", "met"], 70, "APPLY")))
    assert scorer.score(make_rec()).verdict == "MAYBE"


def test_score_clamped_and_bad_verdict_defaults_to_skip():
    scorer = make_scorer(FakeProvider(PASS1, pass2(["met", "met"], 150, "BANANA")))
    ev = scorer.score(make_rec())
    assert ev.relevance_score == 100 and ev.verdict == "SKIP"


def test_caching_prevents_rescoring(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    rec = make_rec()
    db.upsert_job(conn, rec)
    provider = FakeProvider(PASS1, pass2(["met", "met"], 85, "APPLY"))
    scorer = make_scorer(provider)

    first = scorer.score_cached(conn, rec)
    assert first is not None and not first.from_cache and provider.calls == 2

    second = scorer.score_cached(conn, rec)  # same content_hash -> cache hit
    assert second is not None and second.from_cache and provider.calls == 2
    assert second.verdict == "APPLY"


def test_llm_failure_does_not_crash_pipeline(tmp_path):
    class ExplodingProvider(FakeProvider):
        def complete_json(self, *a, **k):
            raise LLMError("boom")

    conn = db.connect(tmp_path / "t.db")
    rec = make_rec()
    db.upsert_job(conn, rec)
    assert make_scorer(ExplodingProvider({}, {})).score_cached(conn, rec) is None


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        'Sure! Here it is:\n```json\n{"a": 1}\n```',
        'preamble {"a": 1} trailing',
    ],
)
def test_json_extraction_tolerates_noise(text):
    assert _extract_json(text) == {"a": 1}


def test_json_extraction_rejects_garbage():
    with pytest.raises(ValueError):
        _extract_json("no json here")


def test_schemas_declare_required_keys():
    assert set(PASS1_SCHEMA["required"]) == {"hard_requirements", "soft_requirements"}
    assert "verdict" in PASS2_SCHEMA["required"]


def test_fallback_provider_switches_on_primary_failure():
    from core.llm import FallbackProvider, LLMError

    class Dead:
        name = "dead"

        def complete_json(self, *a, **k):
            raise LLMError("rate limited")

        def complete_text(self, *a, **k):
            raise LLMError("rate limited")

    class Alive:
        name = "alive"

        def complete_json(self, system, user, schema):
            return {"ok": True}

        def complete_text(self, system, user, max_tokens=16000):
            return "text"

    fb = FallbackProvider(Dead(), Alive())
    assert fb.complete_json("s", "u", {"required": []}) == {"ok": True}
    assert fb.complete_text("s", "u") == "text"


def test_get_provider_uses_whichever_key_exists(monkeypatch):
    from core import llm

    try:  # noqa: SIM105 — some sandboxes have a broken google-genai install
        from google import genai  # noqa: F401
    except BaseException:  # pyo3 panics are BaseException, not ImportError
        pytest.skip("google-genai not importable in this environment")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")  # preferred but unconfigured
    provider = llm.get_provider()
    assert provider.name == "gemini"  # falls through to the configured one


def test_get_provider_errors_when_no_keys(monkeypatch):
    import pytest as _pytest

    from core import llm

    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with _pytest.raises(llm.LLMError):
        llm.get_provider()
