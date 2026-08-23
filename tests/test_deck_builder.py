"""Deck Builder: the Magentic-One-driven conversational deck-building flow
(app/agents.py:DeckBuilderOrchestrator, app/services.py:DeckBuilderService)
that "pptx" chat-trigger messages route into instead of the fixed-question
skill-run form — see CopilotService.chat()'s routing and the HITL
auto_generate toggle (app/models.py:ChatRequest.auto_generate).
"""
import pytest

from app.agents import DeckBuilderOrchestrator, DeckBuilderResult
from app.sandbox import LocalSubprocessSandbox
from app.services import CopilotService, HitlService


def _service(tmp_path) -> CopilotService:
    # Every test gets its own tmp_path-backed data_dir (never the bare
    # no-arg default) so test runs never write into the real repo's data/ —
    # same convention tests/test_checkpointing.py and tests/test_skills.py
    # use throughout.
    return CopilotService(data_dir=tmp_path)


# --- routing: a "pptx" chat-trigger match diverts to Deck Builder -----------

@pytest.mark.asyncio
async def test_pptx_trigger_sets_pending_deck_builder_not_pending_skill_run(tmp_path):
    service = _service(tmp_path)
    result = await service.chat("make me a powerpoint about our roadmap", agent_mode=False, session_id=None)
    sid = result["session_id"]
    session = service.sessions.get(sid)
    assert session.get("pending_skill_run") is None
    assert result["skill_run"] is None
    assert result["agent"] == "deck-builder"


@pytest.mark.asyncio
async def test_no_credentials_degrades_via_fallback_deck_spec(tmp_path):
    # No real model configured (MockProvider/mock ai_mode in tests) ->
    # build_streaming_model_client returns no client -> DeckBuilderOrchestrator
    # must degrade to _fallback_deck_spec rather than crash or hang, same
    # "every skill works end-to-end with zero credentials" guarantee every
    # other skill already has.
    service = _service(tmp_path)
    result = await service.chat("make me a powerpoint about our roadmap", agent_mode=False, session_id=None)
    assert result["response"]  # got SOME usable response, not an exception
    # auto_generate defaults False -> queued for approval, not generated yet.
    pending = service.hitl.list()
    assert any(r["kind"] == "deck_generation" for r in pending)


# --- HITL toggle: auto-generate ON vs OFF ------------------------------------

def _fake_orchestrator(spec: dict):
    class _Fake(DeckBuilderOrchestrator):
        async def run_turn(self, task, context, on_event=None):
            return DeckBuilderResult(spec=spec, provider="mock", model=None)
    return _Fake


_TEST_SPEC = {
    "title": "Q3 Results", "subtitle": "", "theme": "midnight_executive",
    "slides": [{"layout": "bullets", "title": "Overview", "bullets": ["Revenue up 22%"]}],
}


@pytest.mark.asyncio
async def test_auto_generate_on_produces_artifact_immediately(tmp_path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(
        service.deck_builder, "_orchestrator_for", lambda skill: _fake_orchestrator(_TEST_SPEC)(skill),
    )
    result = await service.chat(
        "make me a powerpoint about Q3", agent_mode=False, session_id=None, auto_generate=True,
    )
    assert result["downloadable_artifacts"], "expected an artifact to be produced immediately"
    artifact_id = result["downloadable_artifacts"][0]["artifact_id"]
    stored = service.artifacts.get(artifact_id)
    assert stored.mime_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    # No HITL record created for the auto-generate path.
    assert not any(r["kind"] == "deck_generation" for r in service.hitl.list())
    # pending_deck_builder cleared after successful completion.
    session = service.sessions.get(result["session_id"])
    assert session.get("pending_deck_builder") is None
    assert session.get("last_deck_spec") == _TEST_SPEC


@pytest.mark.asyncio
async def test_auto_generate_off_queues_hitl_approval(tmp_path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(
        service.deck_builder, "_orchestrator_for", lambda skill: _fake_orchestrator(_TEST_SPEC)(skill),
    )
    result = await service.chat(
        "make me a powerpoint about Q3", agent_mode=False, session_id=None, auto_generate=False,
    )
    assert result["downloadable_artifacts"] == []  # nothing generated yet
    pending = [r for r in service.hitl.list() if r["kind"] == "deck_generation"]
    assert len(pending) == 1
    record = pending[0]
    assert record["status"] == "WAITING_FOR_APPROVAL"
    assert record["deck_spec"] == _TEST_SPEC
    assert record["skill_id"] == "pptx"

    decided = await service.hitl.decide(record["request_id"], approved=True)
    assert decided["status"] == "COMPLETED"
    assert decided["downloadable_artifacts"]
    artifact_id = decided["downloadable_artifacts"][0]["artifact_id"]
    stored = service.artifacts.get(artifact_id)
    assert stored.mime_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.mark.asyncio
async def test_decide_rejected_never_runs_generation(tmp_path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(
        service.deck_builder, "_orchestrator_for", lambda skill: _fake_orchestrator(_TEST_SPEC)(skill),
    )
    result = await service.chat(
        "make me a powerpoint about Q3", agent_mode=False, session_id=None, auto_generate=False,
    )
    pending = [r for r in service.hitl.list() if r["kind"] == "deck_generation"][0]

    called = {"ran": False}
    import app.services as services_module

    def fake_run_generation_script(skill, spec):
        called["ran"] = True
        raise AssertionError("run_generation_script must not run for a rejected request")

    monkeypatch.setattr(services_module, "run_generation_script", fake_run_generation_script)
    decided = await service.hitl.decide(pending["request_id"], approved=False)
    assert decided["status"] == "REJECTED"
    assert decided["downloadable_artifacts"] == []
    assert called["ran"] is False


# --- ArtifactStore MIME type -------------------------------------------------

def test_artifact_store_pptx_mime_type(tmp_path):
    from app.artifacts import ArtifactStore
    store = ArtifactStore()
    artifact = store.add("deck.pptx", b"fake bytes")
    assert artifact.mime_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation"


# --- HitlService kind="deck_generation" persistence --------------------------

@pytest.mark.asyncio
async def test_hitl_deck_generation_persists_across_restart(tmp_path):
    from app.skills import SkillPackageStore

    skills = SkillPackageStore(data_dir=tmp_path / "skills")
    hitl = HitlService(LocalSubprocessSandbox(), data_dir=tmp_path / "hitl", skill_store=skills)
    record = hitl.submit_deck_generation(_TEST_SPEC, "pptx", session_id="s1")

    reloaded = HitlService(LocalSubprocessSandbox(), data_dir=tmp_path / "hitl", skill_store=skills)
    fetched = reloaded.get(record["request_id"])
    assert fetched["status"] == "WAITING_FOR_APPROVAL"
    assert fetched["deck_spec"] == _TEST_SPEC
    assert fetched["skill_id"] == "pptx"


# --- session lifecycle: mid-clarification continuation -----------------------

@pytest.mark.asyncio
async def test_pending_deck_builder_persists_across_clarifying_turn(tmp_path, monkeypatch):
    service = _service(tmp_path)

    class _ClarifyThenSpec(DeckBuilderOrchestrator):
        _asked = False

        async def run_turn(self, task, context, on_event=None):
            if not _ClarifyThenSpec._asked:
                _ClarifyThenSpec._asked = True
                return DeckBuilderResult(clarifying_text="Who is the audience for this deck?")
            return DeckBuilderResult(spec=_TEST_SPEC, provider="mock", model=None)

    monkeypatch.setattr(service.deck_builder, "_orchestrator_for", lambda skill: _ClarifyThenSpec(skill))

    first = await service.chat("make me a powerpoint about Q3", agent_mode=False, session_id=None)
    sid = first["session_id"]
    assert "audience" in first["response"].lower()
    session = service.sessions.get(sid)
    pending = session.get("pending_deck_builder")
    assert pending is not None
    assert pending["phase"] == "clarifying"

    second = await service.chat("Executives", agent_mode=False, session_id=sid)
    session_after = service.sessions.get(sid)
    assert session_after.get("pending_deck_builder") is None  # cleared once a spec was ready
    assert second["downloadable_artifacts"] == []  # auto_generate defaulted False -> queued, not generated
