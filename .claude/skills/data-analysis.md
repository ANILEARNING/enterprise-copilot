# Data Analysis Skill

## Trigger
Keyword-selected onto `data-analysis-agent` (`AgentRegistry.select`, `app/agents.py`): `csv`,
`dataset`, `analyze`, `statistic`, `chart`.

## Current implementation status
Unlike `coding` (real HITL code queue, see `coding.md`) and `knowledge-rag` (real hybrid
retrieval pipeline, see `knowledge-rag.md`), `data-analysis` has **no dedicated code path** in
`AutoGenOrchestrator.run` today. Selecting it only changes the `agent`/`skills` label attached to
the response and, if `MAX_OUTPUT_TOKENS_CODE`-style budget tuning were added for it, which
token budget applies — the turn otherwise falls through to the same tool-calling path (if MCP
tools are available and offered) or plain-completion path every other agent uses. There is no
CSV parser, no chart-rendering step, and no dedicated analysis tool wired in yet. Do not assume
prior "analysis output" behavior exists in this codebase — verify against `app/agents.py`
before describing new behavior as already implemented.

## Intended workflow (aspirational — not yet code-backed)
Load → validate → profile → analyze → visualize → verify. If/when this skill gets a real
implementation, follow the same pattern the other two skills already establish:
- A dedicated method on `AutoGenOrchestrator` (mirroring `_queue_generated_code` or the
  retrieval block), not logic scattered across routes/services (`.claude/rules/architecture.md`:
  "prefer reusable skills/tools over duplicated agent logic").
- Any code that actually computes statistics or renders a chart should route through the same
  HITL-gated code execution the coding skill already uses (`coding.md`, `hitl.md`) rather than a
  new, separate execution path — one sandboxed execution mechanism, not two.
- Any real dataset upload/parsing should reuse `app/extraction.py`'s existing sanitize/size-limit
  discipline (already applied to knowledge-base document uploads) rather than a new upload path.

## Constraints
Do not fabricate analysis results, statistics, or chart descriptions the model didn't actually
compute. Without a real execution/computation step behind this skill, a model-only "analysis" is
narrative, not verified output — say so rather than presenting invented numbers as reproducible
findings.

## Completion
Today: a labeled response (`agent: data-analysis-agent`, `skills: ["data-analysis"]`) with
whatever the underlying provider/tool-calling path produced — same honesty rules as `general`.
Once implemented: reproducible findings, and artifacts (charts, tables) only when they were
actually generated through a real, HITL-gated execution step — never asserted without one.
