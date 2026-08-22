"""app/memory.py — buffered + compact-summary context management. Covers
the plain dict-in/dict-out compact_history() (used directly by
AIProvider.complete() call sites) and CompactMemoryState's state round-trip;
CompactingChatCompletionContext (the AutoGen-native flavor) is exercised
live in app/streaming.py's own manual verification since it needs a real
model client — these tests stick to the framework-agnostic core."""
import pytest

from app.memory import CompactMemoryState, compact_history, render_memory_preview
from app.providers import MockProvider


def _turns(n: int) -> list[dict]:
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(n)]


@pytest.mark.asyncio
async def test_short_history_stays_raw_no_summary():
    provider = MockProvider()
    history = _turns(3)  # under buffer_size=5
    context, state = await compact_history(provider, history, CompactMemoryState(), buffer_size=5)
    assert context == history  # nothing overflowed, nothing to summarize
    assert state.summary == ""
    assert state.summarized_count == 0


@pytest.mark.asyncio
async def test_overflow_gets_summarized_and_buffer_stays_raw():
    provider = MockProvider()
    history = _turns(8)  # 3 overflow beyond buffer_size=5
    context, state = await compact_history(provider, history, CompactMemoryState(), buffer_size=5)

    assert context[0]["role"] == "system"
    assert "Summary of earlier conversation" in context[0]["content"]
    # the raw buffered window is exactly the last 5 turns, untouched
    assert context[1:] == history[-5:]
    assert state.summarized_count == 3


@pytest.mark.asyncio
async def test_second_call_only_summarizes_new_overflow():
    provider = MockProvider()
    history = _turns(8)
    _, state = await compact_history(provider, history, CompactMemoryState(), buffer_size=5)
    assert state.summarized_count == 3

    history = history + _turns(2)  # 2 more turns arrive (indices 8, 9 -> "turn 8"/"turn 9")
    _, state2 = await compact_history(provider, history, state, buffer_size=5)
    # overflow is now everything before the last 5 = 5 turns; 3 were already
    # summarized, so only the 2 new ones (indices 3, 4) should have been
    # folded in this call — MockProvider's "summary" is a deterministic echo
    # of the prompt it was given, which quotes the *new* excerpt plus (since
    # one already existed) the prior summary verbatim, not the raw history
    # from scratch.
    assert state2.summarized_count == 5
    assert "turn 3" in state2.summary and "turn 4" in state2.summary
    assert "turn 5" not in state2.summary  # turn 5 is still in the raw buffered window, not summarized


@pytest.mark.asyncio
async def test_summarization_failure_keeps_previous_summary(monkeypatch):
    class FailingProvider(MockProvider):
        async def complete(self, *args, **kwargs):
            raise RuntimeError("simulated summarization outage")

    provider = FailingProvider()
    prior_state = CompactMemoryState(summary="existing summary text", summarized_count=3)
    history = _turns(8)
    context, state = await compact_history(provider, history, prior_state, buffer_size=5)

    # degrades gracefully: previous summary kept, count unchanged, raw buffer still returned
    assert state.summary == "existing summary text"
    assert state.summarized_count == 3
    assert context[0]["content"].endswith("existing summary text")
    assert context[1:] == history[-5:]


def test_compact_memory_state_round_trips_through_dict():
    state = CompactMemoryState(summary="the user likes ramen", summarized_count=12)
    restored = CompactMemoryState.from_dict(state.to_dict())
    assert restored == state


def test_compact_memory_state_from_dict_handles_none_and_empty():
    assert CompactMemoryState.from_dict(None) == CompactMemoryState()
    assert CompactMemoryState.from_dict({}) == CompactMemoryState()


# --- render_memory_preview: powers "View context sent to model" -> memory ----

def test_render_memory_preview_none_when_nothing_to_show():
    assert render_memory_preview([]) is None


def test_render_memory_preview_renders_buffered_turns():
    context = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    preview = render_memory_preview(context)
    assert "user: hi" in preview
    assert "assistant: hello" in preview


def test_render_memory_preview_includes_summary_turn_when_present():
    context = [
        {"role": "system", "content": "Summary of earlier conversation: the user likes ramen"},
        {"role": "user", "content": "what did I say I liked?"},
    ]
    preview = render_memory_preview(context)
    assert "Summary of earlier conversation: the user likes ramen" in preview
    assert "user: what did I say I liked?" in preview


@pytest.mark.asyncio
async def test_render_memory_preview_matches_what_compact_history_actually_sends():
    # End-to-end: what a real call site (AutoGenOrchestrator's plain-
    # completion path) hands to render_memory_preview is exactly
    # compact_history()'s own output — this is the integration point the UI
    # feature depends on, not just the rendering function in isolation.
    provider = MockProvider()
    history = _turns(8)
    context, _ = await compact_history(provider, history, CompactMemoryState(), buffer_size=5)
    preview = render_memory_preview(context)
    assert "Summary of earlier conversation" in preview
    assert "user: turn 6" in preview or "assistant: turn 6" in preview
