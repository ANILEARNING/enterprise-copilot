# HITL Skill

## Trigger
A workflow reaches a configured risky action. Today that's exactly one thing: local code
execution submitted by the coding skill (`HitlService.submit_code_execution`, `app/services.py`
— see `coding.md` for how code gets extracted from the model's answer). `.claude/rules/
security.md`: "Require HITL for configured risky actions" — new risky actions (a future tool
with real side effects) get gated the same way, not given a separate approval mechanism.

## States
`WAITING_FOR_APPROVAL` → `APPROVED`/`REJECTED` → `COMPLETED` (on approval, execution result is
attached) / (rejection has no further state — it stops at `REJECTED`, nothing runs). There is no
`PENDING`/`RUNNING`/`CANCELLED` in the actual implementation — `HitlService.decide()` is the only
transition function, and it moves a record from `WAITING_FOR_APPROVAL` directly to `APPROVED`
(then immediately `COMPLETED`, synchronously, once execution finishes) or `REJECTED`. A request
already decided cannot be decided again (`decide()` raises `ValueError` on that).

## Workflow
1. `HitlService.submit_code_execution(code, session_id)` stores a `WAITING_FOR_APPROVAL` record
   — code does **not** run yet, regardless of who or what submitted it.
2. A human calls `POST /api/hitl/decide` (Agents & Tools tab, or the raw API) with
   `{request_id, approved}`.
3. **Rejected** → status becomes `REJECTED`. Nothing ever executes for this request.
4. **Approved** → `HitlService.decide()` calls `run_approved_code()` (`app/hitl_agents.py`),
   which routes execution through a real `CodeExecutorAgent` wrapping `SandboxCodeExecutor` (an
   adapter over this app's own `CodeSandbox`/`LocalSubprocessSandbox`, `app/sandbox.py`) — the
   **one** place approved code actually runs, regardless of how or when it was approved. Status
   becomes `COMPLETED` with the real `stdout`/`stderr`/`returncode`/`artifacts` attached.
5. **Live wait, SSE only** (`POST /api/chat/stream`, a turn the router sent to the "agent" route):
   because that connection is already long-lived, the turn can genuinely wait for the decision
   instead of only ever reporting "queued". `await_human_decision()` represents the human reviewer as a real
   `UserProxyAgent` whose `input_func` bridges to `HitlService.await_decision()` — resolved the
   moment `/api/hitl/decide` runs for that request, from the same Agents & Tools tab, no separate
   approval path. If approved, the turn's own final answer gets the real execution result
   appended inline; if rejected, a "**Not run — rejected by reviewer.**" note is appended instead.
   Cancelling the wait (the chat's own Stop button) cleanly abandons it — the request stays
   `WAITING_FOR_APPROVAL`, still decidable later. The plain, non-streaming `POST /api/chat` path
   never waits live — it queues and returns immediately, since a bare request can't sensibly stay
   open for arbitrary human approval time.

## What execution actually isolates (and doesn't)
`LocalSubprocessSandbox` (v1's only `CodeSandbox` implementation) provides: a fresh temp
workspace per run, a wall-clock timeout (`MAX_CODE_EXECUTION_SECONDS`), stdout/stderr truncation
(`MAX_OUTPUT_CHARS`), an environment scrubbed of `.env`/app secrets (only `PATH`/`SYSTEMROOT`
survive), a best-effort static guard against naive absolute-path/`os.environ`/`subprocess`/
`socket` access, and artifact-name tracking (files the script created — listed, not persisted
for download in v1; the workspace is deleted once the result is captured). It does **not**
provide real filesystem, network, or process isolation — a determined script can still work
around the static guard (string concatenation, encoding, indirection all trivially bypass a
regex scan). This is explicitly development-only (`docs/security.md`), never a production
sandbox — do not describe it as one in any user-facing text.

## Constraints
- Never let generated code run without a recorded `APPROVED` decision — `run_approved_code` is
  only ever called from `HitlService.decide()` after that decision exists; do not add a second
  call site that bypasses it.
- Never claim code "ran" in a chat response before the human has actually approved it — see
  `coding.md`'s constraint on this exact point.
- The HITL queue and its decisions are in-memory for v1 (`.claude/rules/architecture.md`) — a
  process restart clears pending requests; don't build logic that assumes durability across
  restarts.

## Completion
Persist the decision (`APPROVED`/`REJECTED`) and, on approval, the real execution result —
resume the waiting turn (live SSE path) or leave the record decidable from Agents & Tools (every
other path) safely either way. Never a workflow that silently proceeds past a pending approval.
