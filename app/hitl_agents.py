"""UserProxyAgent + CodeExecutorAgent — real AutoGen agents for the coding
skill's HITL flow, layered on top of HitlService's existing request/decision
bookkeeping (app/services.py), not instead of it: HitlService still owns the
WAITING_FOR_APPROVAL/APPROVED/REJECTED/COMPLETED records and the REST
surface (/api/hitl/*) the Agents & Tools tab already uses — this module only
changes *how* an approval is waited for and *how* approved code runs.

- `run_approved_code()` — what HitlService.decide() calls once a request is
  approved: routes actual execution through a real CodeExecutorAgent wrapping
  SandboxCodeExecutor (this app's own CodeSandbox, app/sandbox.py — same
  safety posture: temp workspace, scrubbed env, timeout, output cap, see
  docs/security.md). The ONE place approved code actually runs, regardless
  of how/when it got approved — no separate/duplicate execution path.
- `await_human_decision()` — what AutoGenOrchestrator's coding-skill path
  (app/agents.py) awaits when a turn can genuinely wait for a live decision
  (agent-mode SSE only — see its own docstring for why). Represents the
  human via a real UserProxyAgent whose `input_func` bridges to
  HitlService.await_decision() — the documented FastAPI/web integration
  pattern for UserProxyAgent (see its own docstring's "For examples of
  integrating with web and UI frameworks: FastAPI..."), adapted to this
  app's queue-then-decide-later REST flow (POST /api/hitl/decide, unchanged)
  rather than an in-process synchronous prompt.

Both stay behind the same AutoGen boundary as app/agents.py and
app/streaming.py (.claude/rules/autogen-maf.md) — nothing outside this file
and those two imports autogen_agentchat.
"""
from __future__ import annotations

import asyncio
import logging

from autogen_agentchat.agents import ApprovalRequest, ApprovalResponse, CodeExecutorAgent, UserProxyAgent
from autogen_agentchat.messages import TextMessage
from autogen_core import CancellationToken
from autogen_core.code_executor import CodeBlock, CodeExecutor, CodeResult

from .sandbox import CodeSandbox, ExecutionResult, ExecutionStatus

logger = logging.getLogger(__name__)


class SandboxCodeExecutor(CodeExecutor):
    """Adapts CodeSandbox (app/sandbox.py) to AutoGen's CodeExecutor
    protocol. Stateless per call — each run gets its own fresh temp
    workspace via CodeSandbox.run(), same as calling it directly; nothing to
    restart/persist between calls."""

    def __init__(self, sandbox: CodeSandbox):
        self._sandbox = sandbox
        # execute_code_blocks() only gets to return AutoGen's much narrower
        # CodeResult (exit_code + output) — this keeps the full, richer
        # ExecutionResult (status/stdout/stderr/artifacts/duration) around
        # for run_approved_code() to hand back unchanged to HitlService,
        # so the REST/UI shape downstream never has to know AutoGen was
        # involved at all.
        self.last_result: ExecutionResult | None = None

    async def execute_code_blocks(self, code_blocks: list[CodeBlock], cancellation_token: CancellationToken) -> CodeResult:
        combined = "\n\n".join(b.code for b in code_blocks)
        # CodeSandbox.run() is a blocking subprocess call — off the event
        # loop so it doesn't stall every other in-flight request meanwhile.
        result = await asyncio.to_thread(self._sandbox.run, combined)
        self.last_result = result
        output = result.stdout or ""
        if result.stderr:
            output = f"{output}\n{result.stderr}" if output else result.stderr
        if result.error and result.status != ExecutionStatus.COMPLETED:
            output = f"{output}\n{result.error}" if output else result.error
        return CodeResult(exit_code=result.returncode if result.returncode is not None else 1, output=output)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def restart(self) -> None:
        pass


async def _already_approved(request: ApprovalRequest) -> ApprovalResponse:
    # Not a bypass: run_approved_code() only ever runs after HitlService.decide()
    # has already recorded a human's APPROVED decision (see its caller) — this
    # just tells CodeExecutorAgent that oversight already happened one layer
    # up, instead of it warning that none occurred at all.
    return ApprovalResponse(approved=True, reason="Approved via HitlService.decide() before this agent ran.")


async def run_approved_code(sandbox: CodeSandbox, code: str) -> ExecutionResult:
    """The ONE place approved code actually executes (called from
    HitlService.decide()) — via a real CodeExecutorAgent, not a bare
    sandbox.run() call, per the coding-agent = CodeExecutorAgent mapping."""
    executor = SandboxCodeExecutor(sandbox)
    agent = CodeExecutorAgent("coding_agent", code_executor=executor, approval_func=_already_approved)
    try:
        await agent.on_messages(
            [TextMessage(content=f"```python\n{code}\n```", source="user")], CancellationToken(),
        )
    except Exception as exc:  # noqa: BLE001 - an agent-layer failure must still report as a normal execution error
        logger.warning("CodeExecutorAgent failed, no result captured: %s", exc)
        return ExecutionResult(status=ExecutionStatus.ERROR, error=f"{type(exc).__name__}: {exc}")
    return executor.last_result or ExecutionResult(
        status=ExecutionStatus.ERROR, error="CodeExecutorAgent produced no result.",
    )


async def await_human_decision(hitl_service, request_id: str) -> dict:
    """Genuinely waits for a human decision on `request_id` via
    HitlService's existing queue-then-decide flow, represented as a real
    UserProxyAgent message exchange (see module docstring) — not a
    fabricated "approved" claim, and not a blocking terminal prompt either.
    Returns the decided record dict (same shape HitlService.get()/list()
    already expose) once /api/hitl/decide resolves it.

    Cancelling the awaiting task (the chat turn's own Stop button, wired via
    CopilotService.chat_stream's cancellation_token.link_future) cleanly
    abandons the wait via UserProxyAgent's own documented cancellation
    support — the request itself stays WAITING_FOR_APPROVAL, still decidable
    later from the Agents & Tools tab regardless of whether anything is
    still listening for the outcome.
    """

    async def wait_for_decision(prompt: str, cancellation_token: CancellationToken | None) -> str:
        record = await hitl_service.await_decision(request_id)
        return record["status"]

    user_proxy = UserProxyAgent("human_reviewer", input_func=wait_for_decision)
    await user_proxy.on_messages(
        [TextMessage(
            content=f"A code execution request (id={request_id}) needs your approval in the "
                    "Agents & Tools tab before this turn can continue.",
            source="coding_agent",
        )],
        CancellationToken(),
    )
    # UserProxyAgent's own reply is just what input_func returned (the
    # decided status string) -- the caller wants the full record (result
    # included), so this reads it back from HitlService directly rather than
    # trying to smuggle a whole dict through a chat message's text content.
    return hitl_service.get(request_id)
