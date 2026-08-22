# HITL (Human-in-the-Loop)

Phase 0 foundation for gating risky actions behind explicit approval.

States: `WAITING_FOR_APPROVAL` → `APPROVED`/`REJECTED` → `COMPLETED` (on approval, execution result is attached).

Current risky action requiring HITL:
- Local code execution (`POST /api/tools/code/submit`)

Flow:

Submit request → stored as `WAITING_FOR_APPROVAL` (not executed)
→ human calls `POST /api/hitl/decide`
→ if approved, the tool runs and the request becomes `COMPLETED` with its result attached
→ if rejected, the request becomes `REJECTED` and nothing runs

The HITL queue and decisions are in-memory for v1, visible in the Tools panel of the SPA.

## Real AutoGen agents (`app/hitl_agents.py`)

Execution and (optionally) the wait itself are real AutoGen agents, layered
on top of the queue/decision bookkeeping above, not instead of it:

- **`CodeExecutorAgent`** — the coding agent's actual execution mechanism.
  `HitlService.decide()` runs approved code through a `CodeExecutorAgent`
  wrapping `SandboxCodeExecutor` (an adapter around this app's own
  `CodeSandbox`, `app/sandbox.py` — identical safety posture: temp
  workspace, scrubbed env, timeout, output cap). The one place approved
  code actually runs, regardless of how it got approved.
- **`UserProxyAgent`** — represents the human reviewer for a *live* wait.
  Agent-mode chat over SSE (`POST /api/chat/stream`) is already a long-lived
  connection, so a coding-skill turn there can genuinely wait for a real
  decision instead of only ever reporting "queued": `UserProxyAgent`'s
  `input_func` bridges to `HitlService.await_decision()`, resolved the
  moment `POST /api/hitl/decide` runs for that request (from the Agents &
  Tools tab, exactly as before — no separate approval path). If approved,
  the turn's final answer incorporates the real execution result. The plain
  non-streaming `POST /api/chat` coding path is unchanged: it queues and
  returns immediately, since a bare POST can't sensibly stay open for
  arbitrary human approval time. Cancelling a live wait (the chat's own Stop
  button) cleanly abandons it — the request stays `WAITING_FOR_APPROVAL`,
  still decidable later from the Agents & Tools tab.
