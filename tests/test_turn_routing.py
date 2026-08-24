"""Autonomous turn routing (app/agents.py:plan_turn) and the three composer
toggles it turned into permissions.

Routing used to be a fixed if/elif chain over SkillPackageStore.
select_for_chat's substring match, evaluated ahead of agent_mode — so Agent
mode, Web search and Auto-generate were mutually exclusive in effect and no
single turn could honour more than one of them. These tests pin down both
halves of the fix: the router decides the route from the query, and all three
toggles now compose on one turn.
"""
import pytest

import app.agents as agents
from app.agents import DeckBuilderOrchestrator, DeckBuilderResult, plan_turn
from app.providers import MockProvider, ProviderResult
from app.services import CopilotService


class _FakeRouterProvider:
    """Stands in for the router model — same shape as tests/test_agent_layer.py's
    equivalent for select_llm. Also used as a non-MockProvider `provider`
    argument, since plan_turn skips the router entirely for a bare mock."""

    def __init__(self, response_text: str | None = None, raises: Exception | None = None):
        self._response_text = response_text
        self._raises = raises

    async def complete(self, prompt, history, max_tokens=None, json_mode=False, images=None):
        if self._raises is not None:
            raise self._raises
        return ProviderResult(text=self._response_text, provider="fake-router")


def _skills(*skill_ids: str) -> list[dict]:
    """SkillPackageStore.list()-shaped entries — plan_turn only reads
    skill_id/name/output/description off these."""
    return [
        {"skill_id": s, "name": s, "output": "pptx" if s == "pptx" else "docx",
         "description": f"Generates a {s} file."}
        for s in skill_ids
    ]


SKILLS = _skills("pptx", "docx-generator", "brd-prd-generator")
PPTX_MATCH = {"skill_id": "pptx", "name": "pptx"}
DOCX_MATCH = {"skill_id": "docx-generator", "name": "docx-generator"}


def _use_router(monkeypatch, router: _FakeRouterProvider) -> None:
    monkeypatch.setattr(agents, "_build_turn_router_provider", lambda: router)


async def _plan(message: str, monkeypatch=None, router=None, *, keyword_match=None,
                allow_agent=True, allow_web=False):
    """plan_turn with a fake router when one is given, and a bare MockProvider
    (which suppresses routing entirely) when one isn't."""
    if router is not None:
        _use_router(monkeypatch, router)
    return await plan_turn(
        message, skills=SKILLS, keyword_match=keyword_match, allow_agent=allow_agent,
        allow_web=allow_web, provider=router if router is not None else MockProvider(),
    )


# --- deterministic fallback: reproduces the pre-router routing exactly --------

@pytest.mark.asyncio
async def test_bare_mock_provider_never_calls_a_router(monkeypatch):
    # Same contract AutoGenOrchestrator.run has for select_llm: explicitly mock
    # means deterministic, so the router must not even be built.
    def _boom():
        raise AssertionError("router built for a bare MockProvider")
    monkeypatch.setattr(agents, "_build_turn_router_provider", _boom)
    plan = await plan_turn(
        "make me a powerpoint", skills=SKILLS, keyword_match=PPTX_MATCH,
        allow_agent=True, allow_web=True, provider=MockProvider(),
    )
    assert plan.route == "deck"
    assert plan.routed_by_llm is False


@pytest.mark.asyncio
async def test_deterministic_pptx_match_routes_to_deck():
    plan = await _plan("make me a powerpoint about Q3", keyword_match=PPTX_MATCH)
    assert (plan.route, plan.skill_id) == ("deck", "pptx")
    # The Deck Builder used to force web_search=True; the fallback records that
    # as "this route wants the web" so nothing is lost when no router exists.
    assert plan.needs_web is True


@pytest.mark.asyncio
async def test_deterministic_other_skill_match_routes_to_skill():
    plan = await _plan("create a report on churn", keyword_match=DOCX_MATCH)
    assert (plan.route, plan.skill_id) == ("skill", "docx-generator")


@pytest.mark.asyncio
async def test_deterministic_no_match_follows_the_agent_toggle():
    assert (await _plan("what is our refund policy", allow_agent=True)).route == "agent"
    assert (await _plan("what is our refund policy", allow_agent=False)).route == "direct"


# --- LLM routing: the decision comes from the query, not from a substring -----

@pytest.mark.asyncio
async def test_router_routes_a_deck_request_that_matches_no_trigger(monkeypatch):
    # "something I can present" contains none of pptx's chat_triggers, so the
    # old substring chain would have sent this to the agent (or a direct
    # answer) and never built anything.
    router = _FakeRouterProvider(
        '{"route": "deck", "skill_id": "pptx", "needs_web": true, '
        '"reason": "wants slides for the board"}'
    )
    plan = await _plan("I need something I can present to the board Thursday", monkeypatch, router)
    assert (plan.route, plan.skill_id) == ("deck", "pptx")
    assert plan.routed_by_llm is True
    assert plan.needs_web is True
    assert plan.reason == "wants slides for the board"


@pytest.mark.asyncio
async def test_router_keeps_a_discussion_question_off_the_generator(monkeypatch):
    # The regression this whole change exists for: "presentation" is a pptx
    # chat_trigger, so asking ABOUT one used to hijack the turn into the Deck
    # Builder — discarding Agent mode and Web search on the way.
    router = _FakeRouterProvider(
        '{"route": "agent", "skill_id": null, "needs_web": false, '
        '"reason": "asking about, not requesting, a deck"}'
    )
    plan = await _plan(
        "what did last quarter's presentation say about churn?", monkeypatch, router,
        keyword_match=PPTX_MATCH,
    )
    assert plan.route == "agent"
    assert plan.skill_id is None


@pytest.mark.asyncio
async def test_router_route_and_skill_id_are_reconciled(monkeypatch):
    # "pptx" is the Deck Builder's conversational flow whatever the router
    # calls it; every other skill is the fixed-question form.
    router = _FakeRouterProvider('{"route": "skill", "skill_id": "pptx", "reason": "slides"}')
    assert (await _plan("slides please", monkeypatch, router)).route == "deck"

    router = _FakeRouterProvider('{"route": "deck", "skill_id": "docx-generator", "reason": "doc"}')
    assert (await _plan("a doc please", monkeypatch, router)).route == "skill"


@pytest.mark.asyncio
async def test_router_cannot_choose_a_route_the_toggles_forbid(monkeypatch):
    # Agent mode off means the agent route isn't on the menu at all; naming it
    # anyway is treated as a bad answer, not as permission.
    router = _FakeRouterProvider('{"route": "agent", "skill_id": null, "reason": "needs tools"}')
    plan = await _plan("plan a migration for us", monkeypatch, router, allow_agent=False)
    assert plan.route == "direct"
    assert plan.routed_by_llm is False


@pytest.mark.asyncio
async def test_router_falls_back_to_trigger_matching_on_bad_answers(monkeypatch):
    for response in ("not json at all", '{"route": "teleport"}', "{}"):
        plan = await _plan(
            "make me a powerpoint about Q3", monkeypatch, _FakeRouterProvider(response),
            keyword_match=PPTX_MATCH,
        )
        assert (plan.route, plan.routed_by_llm) == ("deck", False), response


@pytest.mark.asyncio
async def test_router_falls_back_when_the_call_raises(monkeypatch):
    router = _FakeRouterProvider(raises=RuntimeError("simulated 429"))
    plan = await _plan("create a report on churn", monkeypatch, router, keyword_match=DOCX_MATCH)
    assert (plan.route, plan.skill_id, plan.routed_by_llm) == ("skill", "docx-generator", False)


@pytest.mark.asyncio
async def test_router_naming_an_uninstalled_generator_uses_the_trigger_match(monkeypatch):
    router = _FakeRouterProvider('{"route": "skill", "skill_id": "xlsx-generator", "reason": "a file"}')
    plan = await _plan("build that out for me", monkeypatch, router, keyword_match=DOCX_MATCH)
    assert (plan.route, plan.skill_id) == ("skill", "docx-generator")


@pytest.mark.asyncio
async def test_router_wanting_a_generator_that_does_not_exist_answers_instead(monkeypatch):
    # Nothing real to run and no trigger match to borrow — answering the
    # message beats running the wrong generator against it.
    router = _FakeRouterProvider('{"route": "skill", "skill_id": "xlsx-generator", "reason": "a file"}')
    plan = await _plan("build that out for me", monkeypatch, router, allow_agent=True)
    assert plan.route == "agent"
    assert plan.skill_id is None


@pytest.mark.asyncio
async def test_router_booleans_survive_being_answered_as_strings(monkeypatch):
    router = _FakeRouterProvider('{"route": "agent", "skill_id": null, "needs_web": "true"}')
    assert (await _plan("what shipped in the latest release?", monkeypatch, router)).needs_web is True
    router = _FakeRouterProvider('{"route": "agent", "skill_id": null, "needs_web": "false"}')
    assert (await _plan("what shipped in the latest release?", monkeypatch, router)).needs_web is False


@pytest.mark.asyncio
async def test_router_answer_wrapped_in_a_list_is_still_understood(monkeypatch):
    # Gemini's JSON mode returns the object re-encoded as a string inside a list
    # on some calls and bare on others (verified live). Before _loads_router_json
    # the wrapped shape raised, so routing degraded to trigger matching on
    # roughly every other turn — the feature looked broken rather than absent.
    router = _FakeRouterProvider(
        '["{\\"route\\": \\"deck\\", \\"skill_id\\": \\"pptx\\", \\"reason\\": \\"wants slides\\"}"]'
    )
    plan = await _plan("something for the board Thursday", monkeypatch, router)
    assert (plan.route, plan.skill_id, plan.routed_by_llm) == ("deck", "pptx", True)


# --- the toggles compose on one turn -----------------------------------------

_SPEC = {
    "title": "Q3 Results", "subtitle": "", "theme": "midnight_executive",
    "slides": [{"layout": "bullets", "title": "Overview", "bullets": ["Revenue up 22%"]}],
}
_SOURCE = {"title": "Q3 market data", "url": "https://example.com/q3", "content": "Revenue up."}


def _recording_orchestrator(captured: dict, *, web_sources: list[dict] | None = None):
    class _Fake(DeckBuilderOrchestrator):
        async def run_turn(self, task, context, on_event=None):
            captured["web_search"] = context.get("web_search")
            return DeckBuilderResult(spec=_SPEC, provider="mock", web_sources=web_sources or [])
    return _Fake


@pytest.mark.asyncio
async def test_all_three_toggles_take_effect_on_a_single_deck_turn(tmp_path, monkeypatch):
    """The exact case that was broken: ticking Agent mode + Web search +
    Auto-generate and asking for a deck used to honour only Auto-generate —
    the deck route dropped the other two on the floor."""
    service = CopilotService(data_dir=tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        service.deck_builder, "_orchestrator_for",
        lambda skill: _recording_orchestrator(captured, web_sources=[_SOURCE])(skill),
    )
    result = await service.chat(
        "make me a powerpoint about Q3 with the latest market data",
        agent_mode=True, session_id=None, web_search=True, auto_generate=True,
    )
    assert captured["web_search"] is True, "Web search never reached the deck route"
    assert result["downloadable_artifacts"], "Auto-generate didn't generate"
    assert result["web_sources"] == [_SOURCE], "research the deck read wasn't cited"
    assert result["routing"]["route"] == "deck"


@pytest.mark.asyncio
async def test_deck_route_honours_web_search_being_off(tmp_path, monkeypatch):
    # Previously DeckBuilderOrchestrator.run_turn hardcoded web_search=True, so
    # a deck request searched the live web whether or not the user asked it to.
    service = CopilotService(data_dir=tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        service.deck_builder, "_orchestrator_for",
        lambda skill: _recording_orchestrator(captured)(skill),
    )
    await service.chat("make me a powerpoint about Q3", agent_mode=False, session_id=None,
                       web_search=False)
    assert captured["web_search"] is False


@pytest.mark.asyncio
async def test_web_search_setting_carries_across_a_clarifying_turn(tmp_path, monkeypatch):
    """A deck built over several turns must keep researching on the terms set
    when it started, rather than silently losing the capability mid-flow."""
    service = CopilotService(data_dir=tmp_path)
    captured: dict = {}

    class _Clarifying(DeckBuilderOrchestrator):
        async def run_turn(self, task, context, on_event=None):
            captured["web_search"] = context.get("web_search")
            return DeckBuilderResult(clarifying_text="Who is the audience?", provider="mock")

    monkeypatch.setattr(service.deck_builder, "_orchestrator_for", lambda skill: _Clarifying(skill))
    first = await service.chat("make me a powerpoint about Q3", agent_mode=False, session_id=None,
                               web_search=True)
    sid = first["session_id"]
    assert service.sessions.get(sid)["pending_deck_builder"]["web_search"] is True

    captured.clear()
    # The continuation turn sends no toggles of its own (it's a plain reply).
    await service.chat("Executives", agent_mode=False, session_id=sid)
    assert captured["web_search"] is True


@pytest.mark.asyncio
async def test_pending_deck_builder_without_web_search_field_still_continues(tmp_path, monkeypatch):
    # A pending_deck_builder written before this field existed (or restored
    # from an older checkpoint) must not KeyError mid-conversation.
    service = CopilotService(data_dir=tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        service.deck_builder, "_orchestrator_for",
        lambda skill: _recording_orchestrator(captured)(skill),
    )
    sid = service.sessions.get_or_create(None)["session_id"]
    service.sessions.set_field(sid, "pending_deck_builder", {
        "skill_id": "pptx", "phase": "clarifying", "auto_generate": False,
        "task_brief": "a deck about Q3", "hitl_request_id": None,
    })
    result = await service.chat("Executives", agent_mode=False, session_id=sid)
    assert captured["web_search"] is False
    assert result["routing"] is None, "a continuation isn't a new routing decision"


@pytest.mark.asyncio
async def test_deck_research_survives_a_failed_magentic_one_run(monkeypatch):
    """A Magentic-One run that searches and then dies mid-flight must still
    hand back what it found. Verified live: AutoGen raises RuntimeError
    ("Reflect on tool use produced no valid text response.") on exactly the
    research-heavy turns, and a turn that really had made three Tavily calls
    was reporting zero sources alongside a generic fallback deck."""
    class _Client:
        async def close(self):
            return None

    class _Skill:
        instructions = "Build a deck."

    async def _no_tools(web_search=False):
        return []

    async def _search_then_fail(self, team, task, on_event, web_sources):
        web_sources.append(_SOURCE)
        raise RuntimeError("Reflect on tool use produced no valid text response.")

    monkeypatch.setattr(agents, "build_streaming_model_client",
                        lambda **kwargs: (_Client(), "gemini", "gemini-flash-latest"))
    monkeypatch.setattr(agents, "get_mcp_tools", _no_tools)
    monkeypatch.setattr(agents, "AssistantAgent", lambda *a, **k: object())
    monkeypatch.setattr(agents, "MagenticOneGroupChat", lambda *a, **k: object())
    monkeypatch.setattr(DeckBuilderOrchestrator, "_run_magentic_one", _search_then_fail)

    result = await DeckBuilderOrchestrator(_Skill()).run_turn(
        "a deck on the 2026 EV market", {"web_search": True},
    )
    assert result.used_fallback is True
    assert result.spec is not None, "must still produce a usable deck"
    assert result.web_sources == [_SOURCE], "research done before the failure was thrown away"


@pytest.mark.asyncio
async def test_routing_is_reported_on_an_ordinary_turn(tmp_path):
    service = CopilotService(data_dir=tmp_path)
    result = await service.chat("what is our refund policy", agent_mode=False, session_id=None)
    assert result["routing"]["route"] == "direct"
    assert result["routing"]["routed_by_llm"] is False


@pytest.mark.asyncio
async def test_trigger_word_no_longer_hijacks_a_question_end_to_end(tmp_path, monkeypatch):
    """The whole chain, not just plan_turn: a message containing the "pptx"
    chat_trigger "presentation" used to be routed into the Deck Builder before
    agent_mode was ever read. With a router available it stays an agent turn."""
    service = CopilotService(data_dir=tmp_path)
    # service.provider is a bare MockProvider in tests, which suppresses routing
    # by design — swap it for a non-mock stand-in so the router is consulted.
    # service.orchestrator keeps its own MockProvider, so the agent path still
    # runs offline.
    monkeypatch.setattr(service, "provider", _FakeRouterProvider("unused"))
    _use_router(monkeypatch, _FakeRouterProvider(
        '{"route": "agent", "skill_id": null, "needs_web": false, '
        '"reason": "asking about a deck, not for one"}'
    ))

    def _must_not_run(skill):
        raise AssertionError("Deck Builder ran for a question about a presentation")
    monkeypatch.setattr(service.deck_builder, "_orchestrator_for", _must_not_run)

    result = await service.chat(
        "what did last quarter's presentation say about churn?", agent_mode=True, session_id=None,
    )
    assert result["routing"]["route"] == "agent"
    assert result["routing"]["routed_by_llm"] is True
    assert result["agent"] != "deck-builder"
    assert service.sessions.get(result["session_id"]).get("pending_deck_builder") is None


@pytest.mark.asyncio
async def test_blocked_input_reports_no_routing_decision(tmp_path, monkeypatch):
    service = CopilotService(data_dir=tmp_path)
    monkeypatch.setattr(
        service.guardrails, "check_input",
        lambda message: {"allowed": False, "message": "Blocked.", "redacted_text": None,
                         "matched_rules": ["test"]},
    )
    result = await service.chat("anything at all", agent_mode=False, session_id=None)
    assert result["routing"] is None
