"""Autonomous turn routing (app/agents.py:plan_turn) — no more composer
toggles.

Routing used to be a fixed if/elif chain over SkillPackageStore.
select_for_chat's substring match, evaluated ahead of the old Agent mode /
Web search toggles — so a message could get at most one of "route to a
generator" / "get agent capability" / "search the web", whichever the
substring match or the toggle happened to grant. Agent mode and Web search
are gone entirely now: every turn gets full agent capability (tools, RAG
grounding, live web research whenever Tavily is configured) unconditionally.
The only thing plan_turn still decides is whether a message wants a FILE
generated ("deck"/"skill"), needs augmentation to answer well ("agent"), or
is simple enough to answer directly with no augmentation at all ("direct" —
which exists so CopilotService.chat_stream can real-stream tokens for it).
"""
import httpx
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


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com/fake")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"{status_code} error", request=request, response=response)


class _FlakyThenOkRouterProvider:
    """Fails with a given exception the first `fail_times` calls, then
    succeeds — for testing _complete_with_retry's one-retry behavior against
    a real, transient upstream failure (see that function's docstring)."""

    def __init__(self, response_text: str, *, fail_times: int, exc: Exception):
        self._response_text = response_text
        self._fail_times = fail_times
        self._exc = exc
        self.call_count = 0

    async def complete(self, prompt, history, max_tokens=None, json_mode=False, images=None):
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise self._exc
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


async def _plan(message: str, monkeypatch=None, router=None, *, keyword_match=None):
    """plan_turn with a fake router when one is given, and a bare MockProvider
    (which suppresses routing entirely) when one isn't."""
    if router is not None:
        _use_router(monkeypatch, router)
    return await plan_turn(
        message, skills=SKILLS, keyword_match=keyword_match,
        provider=router if router is not None else MockProvider(),
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
        "make me a powerpoint", skills=SKILLS, keyword_match=PPTX_MATCH, provider=MockProvider(),
    )
    assert plan.route == "deck"
    assert plan.routed_by_llm is False


@pytest.mark.asyncio
async def test_deterministic_pptx_match_routes_to_deck():
    plan = await _plan("make me a powerpoint about Q3", keyword_match=PPTX_MATCH)
    assert (plan.route, plan.skill_id) == ("deck", "pptx")
    # The Deck Builder always researches; the fallback records that as "this
    # route wants the web" so nothing is lost when no router exists.
    assert plan.needs_web is True


@pytest.mark.asyncio
async def test_deterministic_other_skill_match_routes_to_skill():
    plan = await _plan("create a report on churn", keyword_match=DOCX_MATCH)
    assert (plan.route, plan.skill_id) == ("skill", "docx-generator")


@pytest.mark.asyncio
async def test_deterministic_no_match_routes_to_the_agent():
    # No toggle to consult any more — the deterministic fallback (no router
    # available) always grants full agent capability rather than guessing at
    # whether a message needs it.
    assert (await _plan("what is our refund policy")).route == "agent"


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
    # Builder.
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
async def test_router_sends_a_conversion_request_to_the_agent_not_the_generator(monkeypatch):
    # A live regression: "convert this into a docx" contains docx-generator's
    # "docx" chat_trigger, but the user means "package what's ALREADY in this
    # conversation as a file" — docx-generator's own Q&A form starts from a
    # blank topic/audience/tone form and knows nothing about "this." Only the
    # coding agent can take arbitrary existing content and write real code to
    # package it. The router is told this distinction directly in its prompt
    # (see plan_turn's "EXISTING content" instruction) — this test pins the
    # LLM's reasoning being honored, not a hand-coded keyword override.
    router = _FakeRouterProvider(
        '{"route": "agent", "skill_id": null, "needs_web": false, '
        '"reason": "converting existing content, not drafting new"}'
    )
    plan = await _plan(
        "can you convert this into a docx for me?", monkeypatch, router,
        keyword_match=DOCX_MATCH,
    )
    assert plan.route == "agent"
    assert plan.skill_id is None


@pytest.mark.asyncio
async def test_router_can_choose_direct_for_a_simple_question(monkeypatch):
    # "direct" is always on the menu now — no toggle gates it — and is the
    # router's own signal that a message needs no augmentation at all.
    router = _FakeRouterProvider('{"route": "direct", "skill_id": null, "reason": "simple arithmetic"}')
    plan = await _plan("what is 12 * 7?", monkeypatch, router)
    assert plan.route == "direct"
    assert plan.routed_by_llm is True


@pytest.mark.asyncio
async def test_router_route_and_skill_id_are_reconciled(monkeypatch):
    # "pptx" is the Deck Builder's conversational flow whatever the router
    # calls it; every other skill is the fixed-question form.
    router = _FakeRouterProvider('{"route": "skill", "skill_id": "pptx", "reason": "slides"}')
    assert (await _plan("slides please", monkeypatch, router)).route == "deck"

    router = _FakeRouterProvider('{"route": "deck", "skill_id": "docx-generator", "reason": "doc"}')
    assert (await _plan("a doc please", monkeypatch, router)).route == "skill"


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
    # message beats running the wrong generator against it. Degrades to the
    # fully-capable "agent" route, not "direct" — the safer default.
    router = _FakeRouterProvider('{"route": "skill", "skill_id": "xlsx-generator", "reason": "a file"}')
    plan = await _plan("build that out for me", monkeypatch, router)
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


# --- router retry on a transient upstream failure (_complete_with_retry) -----
#
# Live regression: Gemini's own API returning a genuine 503 on the router
# call was silently degrading EVERY turn to trigger matching (zero reasoning)
# for as long as it stayed flaky — the exact condition where "docx" in
# "can you make this above as a docx" always won, with no way to tell "draft
# new" from "convert existing content" apart (that distinction lives entirely
# in the router's own reasoning, see plan_turn's prompt). A single retry on a
# transient status keeps real reasoning in the loop instead of falling back
# to none the moment Gemini hiccups once.

@pytest.mark.asyncio
async def test_router_retries_once_on_503_then_succeeds(monkeypatch):
    monkeypatch.setattr(agents.asyncio, "sleep", lambda *a, **k: _noop())
    router = _FlakyThenOkRouterProvider(
        '{"route": "agent", "skill_id": null, "reason": "ok on retry"}',
        fail_times=1, exc=_http_status_error(503),
    )
    plan = await _plan("can you make this above as a docx", monkeypatch, router, keyword_match=DOCX_MATCH)
    assert plan.route == "agent"
    assert plan.routed_by_llm is True
    assert router.call_count == 2


@pytest.mark.asyncio
async def test_router_retries_once_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(agents.asyncio, "sleep", lambda *a, **k: _noop())
    router = _FlakyThenOkRouterProvider(
        '{"route": "direct", "skill_id": null, "reason": "ok on retry"}',
        fail_times=1, exc=_http_status_error(429),
    )
    plan = await _plan("what is 2+2", monkeypatch, router)
    assert plan.route == "direct"
    assert router.call_count == 2


@pytest.mark.asyncio
async def test_router_does_not_retry_a_non_transient_status(monkeypatch):
    # A 401 (bad credentials) would just fail identically again — retrying it
    # only adds latency for no benefit, so it must degrade immediately, same
    # as before this retry existed.
    router = _FlakyThenOkRouterProvider(
        '{"route": "agent", "skill_id": null}', fail_times=1, exc=_http_status_error(401),
    )
    plan = await _plan("write a report on churn", monkeypatch, router, keyword_match=DOCX_MATCH)
    assert (plan.route, plan.routed_by_llm) == ("skill", False)  # degraded to the deterministic fallback
    assert router.call_count == 1


@pytest.mark.asyncio
async def test_router_degrades_after_a_second_consecutive_503(monkeypatch):
    # The retry is bounded to one attempt — a router that's still failing
    # after that must degrade exactly like before, not retry forever.
    monkeypatch.setattr(agents.asyncio, "sleep", lambda *a, **k: _noop())
    router = _FlakyThenOkRouterProvider(
        '{"route": "agent", "skill_id": null}', fail_times=2, exc=_http_status_error(503),
    )
    plan = await _plan("write a report on churn", monkeypatch, router, keyword_match=DOCX_MATCH)
    assert (plan.route, plan.routed_by_llm) == ("skill", False)
    assert router.call_count == 2


async def _noop():
    return None


# --- delivery: file vs. inline content (TurnPlan.delivery) --------------------

@pytest.mark.asyncio
async def test_deterministic_fallback_always_delivers_file():
    # No router available -> the safe, today's-behaviour default. Telling
    # "just tell me" apart from an ordinary file request is exactly the
    # judgment call that needs a real router.
    plan = await _plan("make me a powerpoint about Q3", keyword_match=PPTX_MATCH)
    assert plan.delivery == "file"


@pytest.mark.asyncio
async def test_router_can_choose_inline_delivery(monkeypatch):
    router = _FakeRouterProvider(
        '{"route": "skill", "skill_id": "docx-generator", "delivery": "inline", '
        '"reason": "just wants the content here"}'
    )
    plan = await _plan("just tell me what a good memo about Q3 would say, no need for a file",
                       monkeypatch, router, keyword_match=DOCX_MATCH)
    assert plan.route == "skill"
    assert plan.delivery == "inline"


@pytest.mark.asyncio
async def test_router_delivery_defaults_to_file_when_ambiguous(monkeypatch):
    router = _FakeRouterProvider('{"route": "deck", "skill_id": "pptx", "reason": "wants slides"}')
    plan = await _plan("make me a powerpoint about Q3", monkeypatch, router, keyword_match=PPTX_MATCH)
    assert plan.delivery == "file"


@pytest.mark.asyncio
async def test_router_delivery_ignores_garbage_values(monkeypatch):
    router = _FakeRouterProvider(
        '{"route": "skill", "skill_id": "docx-generator", "delivery": "as a PDF please"}'
    )
    plan = await _plan("write me a report", monkeypatch, router, keyword_match=DOCX_MATCH)
    assert plan.delivery == "file"


@pytest.mark.asyncio
async def test_router_delivery_is_ignored_on_non_file_routes(monkeypatch):
    # A hallucinated "delivery": "inline" on an "agent"/"direct" answer must
    # not leak through — it means nothing on those routes.
    router = _FakeRouterProvider('{"route": "agent", "skill_id": null, "delivery": "inline"}')
    plan = await _plan("what is our refund policy", monkeypatch, router)
    assert plan.route == "agent"
    assert plan.delivery == "file"


# --- deck research (no more per-turn web_search toggle) -----------------------

_SPEC = {
    "title": "Q3 Results", "subtitle": "", "theme": "midnight_executive",
    "slides": [{"layout": "bullets", "title": "Overview", "bullets": ["Revenue up 22%"]}],
}
_SOURCE = {"title": "Q3 market data", "url": "https://example.com/q3", "content": "Revenue up."}


def _recording_orchestrator(captured: dict, *, web_sources: list[dict] | None = None):
    class _Fake(DeckBuilderOrchestrator):
        async def run_turn(self, task, context, on_event=None):
            captured["ran"] = True
            return DeckBuilderResult(spec=_SPEC, provider="mock", web_sources=web_sources or [])
    return _Fake


@pytest.mark.asyncio
async def test_auto_generate_still_takes_effect_on_a_deck_turn(tmp_path, monkeypatch):
    service = CopilotService(data_dir=tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        service.deck_builder, "_orchestrator_for",
        lambda skill: _recording_orchestrator(captured, web_sources=[_SOURCE])(skill),
    )
    result = await service.chat(
        "make me a powerpoint about Q3 with the latest market data",
        session_id=None, auto_generate=True,
    )
    assert captured["ran"], "Deck Builder never ran"
    assert result["downloadable_artifacts"], "Auto-generate didn't generate"
    assert result["web_sources"] == [_SOURCE], "research the deck read wasn't cited"
    assert result["routing"]["route"] == "deck"


@pytest.mark.asyncio
async def test_deck_route_inline_delivery_renders_text_no_file(tmp_path, monkeypatch):
    # pptx has a renderer (app/skill_render.py) — "inline" delivery must
    # render the spec as chat text and produce no downloadable artifact and
    # no HITL queue entry at all.
    from app.agents import TurnPlan
    service = CopilotService(data_dir=tmp_path)
    monkeypatch.setattr(
        service.deck_builder, "_orchestrator_for",
        lambda skill: _recording_orchestrator({})(skill),
    )
    plan = TurnPlan(route="deck", skill_id="pptx", delivery="inline", routed_by_llm=True, reason="test")
    result = await service.chat("just tell me what a Q3 deck would say", session_id=None, plan=plan)
    assert result["downloadable_artifacts"] == []
    assert result["hitl_pending"] == []
    assert "# Q3 Results" in result["response"]
    assert "Revenue up 22%" in result["response"]


@pytest.mark.asyncio
async def test_skill_route_inline_delivery_skips_generation(tmp_path, monkeypatch):
    # docx-generator also has a renderer — a chat-integrated skill run with
    # delivery="inline" must render the final answer as text instead of
    # generating and uploading a file, once its questions are all answered.
    from app.agents import TurnPlan
    service = CopilotService(data_dir=tmp_path)

    async def _fake_draft_spec(skill, answers, provider):
        return (
            {"title": "Q3 Update", "sections": [{"heading": "Summary", "paragraphs": ["All good."]}]},
            "mock", False,
        )
    monkeypatch.setattr("app.skills.draft_spec", _fake_draft_spec)

    docx_skill = service.skill_packages.get("docx-generator")
    plan = TurnPlan(route="skill", skill_id="docx-generator", delivery="inline", routed_by_llm=True, reason="test")
    first = await service.chat("just tell me what a Q3 report would say", session_id=None, plan=plan)
    sid = first["session_id"]

    # Answer every required question in turn until the run finishes.
    result = first
    guard = 0
    while result["skill_run"] and result["skill_run"]["status"] == "AWAITING_ANSWERS" and guard < 20:
        result = await service.chat("Q3 performance", session_id=sid)
        guard += 1

    assert result["skill_run"]["status"] == "COMPLETED_INLINE"
    assert result["skill_run"]["rendered_text"]
    assert "Summary" in result["skill_run"]["rendered_text"]
    assert result["skill_run"]["outputs"] == [], "no file should have been produced"
    assert not result["skill_run"]["download_ready"]


@pytest.mark.asyncio
async def test_deck_route_always_offers_web_research(monkeypatch):
    # DeckBuilderOrchestrator.run_turn always offers the Tavily tool now — no
    # per-turn toggle left to gate it (see get_mcp_tools's call site there).
    captured: dict = {}

    async def _tools_offered(web_search=False):
        captured["web_search"] = web_search
        return []

    class _Client:
        async def close(self):
            return None

    async def _no_magentic_one(self, team, task, on_event, web_sources):
        return "{}"

    monkeypatch.setattr(agents, "get_mcp_tools", _tools_offered)
    monkeypatch.setattr(
        agents, "build_streaming_model_client", lambda **kwargs: (_Client(), "gemini", "gemini-flash-latest"),
    )
    monkeypatch.setattr(agents, "AssistantAgent", lambda *a, **k: object())
    monkeypatch.setattr(agents, "MagenticOneGroupChat", lambda *a, **k: object())
    monkeypatch.setattr(DeckBuilderOrchestrator, "_run_magentic_one", _no_magentic_one)
    await DeckBuilderOrchestrator(type("S", (), {"instructions": "Build a deck."})()).run_turn(
        "make me a powerpoint about Q3", {"session_id": "s1"},
    )
    assert captured["web_search"] is True


@pytest.mark.asyncio
async def test_pending_deck_builder_continues_without_a_web_search_field(tmp_path, monkeypatch):
    # A pending_deck_builder written before web_search existed, or after it was
    # removed, must not KeyError mid-conversation.
    service = CopilotService(data_dir=tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        service.deck_builder, "_orchestrator_for",
        lambda skill: _recording_orchestrator(captured)(skill),
    )
    session = await service.sessions.get_or_create(None)
    sid = session["session_id"]
    await service.sessions.set_field(sid, "pending_deck_builder", {
        "skill_id": "pptx", "phase": "clarifying", "auto_generate": False,
        "task_brief": "a deck about Q3", "hitl_request_id": None,
    })
    result = await service.chat("Executives", session_id=sid)
    assert captured["ran"]
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

    result = await DeckBuilderOrchestrator(_Skill()).run_turn("a deck on the 2026 EV market", {})
    assert result.used_fallback is True
    assert result.spec is not None, "must still produce a usable deck"
    assert result.web_sources == [_SOURCE], "research done before the failure was thrown away"


# --- routing is reported end-to-end -------------------------------------------

@pytest.mark.asyncio
async def test_routing_is_reported_on_an_ordinary_turn(tmp_path):
    # service.provider is a bare MockProvider in tests, which suppresses
    # routing by design — the deterministic fallback always grants "agent".
    service = CopilotService(data_dir=tmp_path)
    result = await service.chat("what is our refund policy", session_id=None)
    assert result["routing"]["route"] == "agent"
    assert result["routing"]["routed_by_llm"] is False


@pytest.mark.asyncio
async def test_trigger_word_no_longer_hijacks_a_question_end_to_end(tmp_path, monkeypatch):
    """The whole chain, not just plan_turn: a message containing the "pptx"
    chat_trigger "presentation" used to be routed into the Deck Builder before
    the router was ever consulted. With a router available it stays an agent
    turn."""
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

    result = await service.chat("what did last quarter's presentation say about churn?", session_id=None)
    assert result["routing"]["route"] == "agent"
    assert result["routing"]["routed_by_llm"] is True
    assert result["agent"] != "deck-builder"
    session = await service.sessions.get(result["session_id"])
    assert session.get("pending_deck_builder") is None


@pytest.mark.asyncio
async def test_blocked_input_reports_no_routing_decision(tmp_path, monkeypatch):
    service = CopilotService(data_dir=tmp_path)
    monkeypatch.setattr(
        service.guardrails, "check_input",
        lambda message: {"allowed": False, "message": "Blocked.", "redacted_text": None,
                         "matched_rules": ["test"]},
    )
    result = await service.chat("anything at all", session_id=None)
    assert result["routing"] is None
