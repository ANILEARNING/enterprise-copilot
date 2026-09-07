# Coding Skill

## Trigger
Keyword-selected onto `coding-agent` (`AgentRegistry.select`, `app/agents.py`): `code`, `bug`,
`function`, `script`, `debug`, `implement`, or a literal `` ``` `` in the message. No LLM call
decides this — it's a deterministic substring match, checked before any model runs.

## Workflow
Inspect → implement → test → fix → validate — same as the general engineering discipline this
project's own `CLAUDE.md` workflow follows. Concretely, on the router's "agent" route:

1. The turn's system prompt (`_build_agent_mode_system_message`, `app/agents.py`) is extended
   with `_CODING_SYSTEM_ADDENDUM`, which mandates two things the model must not skip:
   - Any runnable code in the answer **must** be inside a ` ```language ... ``` ` fence.
   - Even when no bug is found and the user's code is already correct, the full working code
     must still be restated in a fence — never described in prose only. There must always be a
     concrete block for a human to approve, or nothing can ever run.
2. The model answers (optionally calling MCP tools first, e.g. `web_search` if offered and the
   task needs current information — see `web-search.md`).
3. `AutoGenOrchestrator._queue_generated_code` (`app/agents.py`) extracts a fenced code block
   from the model's **own answer** — not the user's original task — via `extract_code_block`
   (a ` ``` ` regex). This is deliberate: "debug this ```code```" queues whatever corrected
   version the model produced, not blindly the user's original draft.
4. If no fence is found, `_looks_like_unfenced_code` runs as a belt-and-suspenders fallback: it
   anchors on a line that opens a definition/import/control-flow construct (`def`, `class`,
   `import`, `function`, `const`/`let`/`var =`, a Java/C#-style method signature, or an `if`/
   `for`/`while` block) and extends through whatever's indented or blank underneath, stopping at
   the first column-0 line that isn't itself an opener. This exists because a live model does
   not reliably follow instruction (1) 100% of the time — the queue must not silently do nothing
   just because the model ignored the fence instruction. A heuristic catch is flagged
   `heuristic: true` on the `code_queued` event so a reviewer knows it wasn't an explicit fence.
5. Extracted code is **queued, never executed inline** — `HitlService.submit_code_execution`
   creates a `WAITING_FOR_APPROVAL` record. See `hitl.md` for what happens next.

## Constraints
- Never claim to have run code — the model can only write it; a human must approve it first.
  (`_CODING_SYSTEM_ADDENDUM` states this explicitly, because a model narrating "I ran this and
  got..." before approval is a direct guardrail/trust violation, not just imprecise phrasing.)
- If the model answers with no code at all (a conceptual question, not an implementation
  request), nothing is queued — `extract_code_block` and the fallback both return `None`, and
  `_queue_generated_code` is a no-op. Not every coding-agent turn produces a HITL request.
- Execution, when it happens, is development-only — see `hitl.md` and `docs/security.md` for
  what `CodeSandbox`/`LocalSubprocessSandbox` actually isolates (and doesn't).
- `max_output_tokens_code` (`app/config.py`, default 6144) overrides the default token budget
  for the plain-completion path specifically — coding answers routinely exceed the generic
  256-token default. The tool-calling path (`_run_with_tools`) has no equivalent per-call
  override yet (AutoGen's `ChatCompletionClient` doesn't take one per-request the way
  `AIProvider.complete()` does).

## Completion
Working, fenced code queued for human approval (`hitl_pending` non-empty in the response), or an
explicit answer with nothing to queue (a question was answered, no code was warranted). Never a
turn that silently drops code the model wrote.
