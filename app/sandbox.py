"""Code execution sandbox abstraction.

`CodeSandbox` is the stable contract; `LocalSubprocessSandbox` is the v1
implementation. Mirrors the AIProvider/EmbeddingProvider swap pattern in
app/providers.py and the AgentOrchestrator/AutoGenOrchestrator boundary in
app/agents.py (see .claude/rules/autogen-maf.md): callers (HitlService,
routes) depend only on CodeSandbox, so a hardened backend — Docker, gVisor,
Firecracker, or similar — can implement the same contract and replace
LocalSubprocessSandbox later without touching anything upstream.

IMPORTANT — read before trusting this with anything adversarial:
LocalSubprocessSandbox is NOT a secure sandbox. It runs code as a plain OS
subprocess of the current interpreter. There is no seccomp/namespace/VM/
container boundary. A sufficiently determined script can still open an
absolute path elsewhere on disk, make network calls, or spawn further
subprocesses — none of that is actually prevented, only the narrower things
listed on LocalSubprocessSandbox are. This is a deliberate development-only
trade-off (`.claude/rules/security.md`: "local subprocess execution is
development-only, not a production sandbox"), not an oversight.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 10000
MAX_ARTIFACTS_LISTED = 20
# Downloadable-artifact capture (see ExecutionResult.artifact_files below):
# only these extensions are ever read into memory and kept past the
# workspace's lifetime — a deliberately narrow allowlist, not "any file the
# script wrote." Keeps this feature scoped to what it's actually for
# (a dashboard/report the user asked the agent to produce), not a general
# file-exfiltration channel out of the sandbox.
DOWNLOADABLE_ARTIFACT_EXTENSIONS = (".html", ".htm", ".pdf")
MAX_ARTIFACT_FILE_BYTES = 15 * 1024 * 1024  # 15MB per file
MAX_ARTIFACT_FILES_CAPTURED = 3


class ExecutionStatus:
    """Sandbox-level outcome — distinct from whether the script itself
    succeeded (see ExecutionResult.ok/returncode). A script that runs to
    completion and then raises is still status=COMPLETED, ok=False."""

    COMPLETED = "completed"  # ran to termination; ok/returncode reflect the script's own result
    TIMEOUT = "timeout"      # killed for exceeding the configured timeout
    DISABLED = "disabled"    # code execution is turned off in this environment
    BLOCKED = "blocked"      # rejected by the pre-execution static guard, never ran
    ERROR = "error"          # sandbox-level failure unrelated to the script's own logic


@dataclass
class ExecutionResult:
    status: str
    ok: bool = False
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    duration_seconds: float | None = None
    # filename -> raw bytes, for whichever `artifacts` entries matched
    # DOWNLOADABLE_ARTIFACT_EXTENSIONS — read from the workspace before it's
    # deleted (CodeSandbox.run()'s finally: shutil.rmtree). Not part of
    # public() (see below): this is real file content, potentially several
    # MB, and belongs in the artifact store (app/artifacts.py) once
    # HitlService.decide() registers it there — not echoed back verbatim in
    # every HITL status/list response.
    artifact_files: dict[str, bytes] = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "status": self.status, "ok": self.ok, "stdout": self.stdout, "stderr": self.stderr,
            "returncode": self.returncode, "artifacts": self.artifacts, "error": self.error,
            "duration_seconds": self.duration_seconds,
        }


class CodeSandbox:
    """The contract application code depends on. See module docstring.

    `run` is async — E2BSandbox's real backend (https://e2b.dev) is
    async-native (its SDK's AsyncSandbox, not a sync wrapper around it), so
    the interface follows that rather than forcing every implementation to
    bridge sync/async on its own. LocalSubprocessSandbox's own blocking
    subprocess.run() call is wrapped in asyncio.to_thread internally (see
    its own run()) — that bridging is this sandbox's concern, not its
    caller's; SandboxCodeExecutor (app/hitl_agents.py) just awaits
    whichever sandbox it was given."""

    async def run(self, code: str) -> ExecutionResult:
        raise NotImplementedError


# Best-effort static guard against a script naively reaching outside its
# workspace (an absolute-path open()) or trying to read process env/secrets.
# This is NOT sandboxing: string concatenation, base64/hex-encoded source,
# getattr() indirection, or exec(compile(...)) trivially bypass a regex scan.
# It exists to catch naive/accidental attempts, not a determined adversary —
# see the module docstring's "not a secure sandbox" note.
_SUSPICIOUS_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\.env\b"),
    re.compile(r"os\.environ"),
    re.compile(r"""open\s*\(\s*["'](?:[A-Za-z]:[\\/]|/)"""),  # open() with an absolute path
    re.compile(r"os\.chdir\s*\("),
    re.compile(r"subprocess\."),
    re.compile(r"socket\."),
)


def _first_suspicious_match(code: str) -> str | None:
    for pattern in _SUSPICIOUS_PATTERNS:
        if pattern.search(code):
            return pattern.pattern
    return None


class LocalSubprocessSandbox(CodeSandbox):
    """Development-only: runs Python source as a subprocess of the current
    interpreter, in a fresh temp directory, with a scrubbed environment.

    Provides — and only provides:
    - workspace isolation: a throwaway temp dir per run, removed afterward
    - a wall-clock timeout (settings.max_code_execution_seconds)
    - an output size cap (stdout/stderr each truncated to MAX_OUTPUT_CHARS)
    - environment scrubbing: no .env/secrets passthrough, only PATH/SYSTEMROOT
    - a best-effort static guard against naive absolute-path/env access
      (see _SUSPICIOUS_PATTERNS above — explicitly not real sandboxing)
    - artifact tracking: files the script created inside its own workspace
      are listed by name (not persisted for later download in v1 — the
      workspace is removed once the result is captured)

    Does NOT provide real filesystem, network, or process isolation.
    """

    async def run(self, code: str) -> ExecutionResult:
        if settings.code_execution_mode != "local":
            return ExecutionResult(status=ExecutionStatus.DISABLED,
                                    error="Code execution is disabled in this environment.")

        matched = _first_suspicious_match(code)
        if matched:
            return ExecutionResult(
                status=ExecutionStatus.BLOCKED,
                error=f"Blocked by the pre-execution guard (matched pattern {matched!r}). "
                      "This is a best-effort static check, not sandboxing — see app/sandbox.py.",
            )

        try:
            workspace = tempfile.mkdtemp(prefix="copilot-exec-")
        except OSError as exc:
            return ExecutionResult(status=ExecutionStatus.ERROR, error=f"Could not create workspace: {exc}")

        try:
            # _run_in_workspace's subprocess.run() call is blocking — off the
            # event loop so it doesn't stall every other in-flight request
            # meanwhile. Was previously the caller's job (SandboxCodeExecutor,
            # app/hitl_agents.py) — moved here so every CodeSandbox
            # implementation's own async/sync bridging is its own concern
            # (see CodeSandbox.run's docstring).
            return await asyncio.to_thread(self._run_in_workspace, code, Path(workspace))
        finally:
            shutil.rmtree(workspace, ignore_errors=True)  # best-effort; a locked file shouldn't fail the request

    def _run_in_workspace(self, code: str, workspace_path: Path) -> ExecutionResult:
        script_path = workspace_path / "snippet.py"
        script_path.write_text(code, encoding="utf-8")
        before = set(workspace_path.iterdir())

        # Strip the parent environment so app secrets (.env, API keys) are never
        # visible to executed code, per project security rules.
        safe_env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        }

        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=str(workspace_path),
                capture_output=True,
                text=True,
                timeout=settings.max_code_execution_seconds,
                env=safe_env,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(
                status=ExecutionStatus.TIMEOUT,
                stdout=_truncate(exc.stdout),
                stderr=_truncate(exc.stderr),
                error=f"Execution exceeded {settings.max_code_execution_seconds}s timeout.",
                duration_seconds=time.monotonic() - started,
            )
        except OSError as exc:
            return ExecutionResult(status=ExecutionStatus.ERROR, error=f"Could not start execution: {exc}")

        duration = time.monotonic() - started
        after = set(workspace_path.iterdir())
        artifacts = sorted(p.name for p in (after - before) if p.name != "snippet.py")

        # Read qualifying artifact content into memory now, while the
        # workspace still exists — run()'s finally block deletes it right
        # after this method returns. Only .html/.pdf, only if the script
        # actually succeeded (a failed run's half-written file isn't worth
        # keeping), only the first MAX_ARTIFACT_FILES_CAPTURED, only up to
        # MAX_ARTIFACT_FILE_BYTES each (silently skipped if larger — still
        # listed in `artifacts`, just not offered as a download).
        artifact_files: dict[str, bytes] = {}
        if proc.returncode == 0:
            for name in artifacts:
                if len(artifact_files) >= MAX_ARTIFACT_FILES_CAPTURED:
                    break
                if not name.lower().endswith(DOWNLOADABLE_ARTIFACT_EXTENSIONS):
                    continue
                path = workspace_path / name
                try:
                    if path.stat().st_size > MAX_ARTIFACT_FILE_BYTES:
                        continue
                    artifact_files[name] = path.read_bytes()
                except OSError:
                    continue  # unreadable (permissions, race, symlink oddity) — skip, don't fail the whole result

        return ExecutionResult(
            status=ExecutionStatus.COMPLETED,
            ok=proc.returncode == 0,
            stdout=_truncate(proc.stdout),
            stderr=_truncate(proc.stderr),
            returncode=proc.returncode,
            artifacts=artifacts[:MAX_ARTIFACTS_LISTED],
            artifact_files=artifact_files,
            duration_seconds=duration,
        )


# Files already present in a fresh E2B sandbox's home directory — verified
# live (AsyncSandbox.create() then files.list('/home/user') before running
# any code) — subtracted from a post-run listing the same way
# LocalSubprocessSandbox diffs before/after its own workspace, so these
# don't show up as "artifacts" the script supposedly produced.
_E2B_BASELINE_HOME_FILES = frozenset({
    ".bash_logout", ".bashrc", ".profile", ".sudo_as_admin_successful",
})
_E2B_HOME_DIR = "/home/user"


class E2BSandbox(CodeSandbox):
    """A real, network-isolated VM sandbox via the E2B API
    (https://e2b.dev/docs) — code_execution_mode == "e2b" (see
    app/config.py). Unlike LocalSubprocessSandbox this genuinely isolates
    the executed code: a dedicated micro-VM per run, not a subprocess of
    this app's own interpreter, so none of that class's "does NOT provide
    real ... isolation" caveats apply here.

    Each call opens a fresh sandbox and kills it when done — no sandbox
    reuse across calls in this v1 (a longer-lived pool/session concept is a
    natural follow-up once there's a real multi-step "agent working in one
    sandbox over several turns" use case; today's one-shot HITL-approved
    code execution, app/hitl_agents.py, doesn't need one)."""

    def __init__(self, api_key: str, timeout_seconds: int):
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def run(self, code: str) -> ExecutionResult:
        if settings.code_execution_mode != "e2b":
            return ExecutionResult(status=ExecutionStatus.DISABLED,
                                    error="Code execution is disabled in this environment.")
        if not self._api_key:
            return ExecutionResult(status=ExecutionStatus.ERROR, error="E2B_SANDBOX is not configured.")

        matched = _first_suspicious_match(code)
        if matched:
            return ExecutionResult(
                status=ExecutionStatus.BLOCKED,
                error=f"Blocked by the pre-execution guard (matched pattern {matched!r}). "
                      "This is a best-effort static check, not sandboxing — see app/sandbox.py.",
            )

        from e2b import FileType
        from e2b_code_interpreter import AsyncSandbox
        from e2b_code_interpreter.exceptions import TimeoutException

        started = time.monotonic()
        try:
            sbx = await AsyncSandbox.create(api_key=self._api_key, timeout=self._timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - sandbox creation failure (bad key, quota, network) is not this app's bug
            return ExecutionResult(status=ExecutionStatus.ERROR, error=f"Could not start E2B sandbox: {exc}")

        try:
            try:
                execution = await sbx.run_code(code, timeout=self._timeout_seconds)
            except TimeoutException:
                return ExecutionResult(
                    status=ExecutionStatus.TIMEOUT,
                    error=f"Execution exceeded {self._timeout_seconds}s timeout.",
                    duration_seconds=time.monotonic() - started,
                )

            duration = time.monotonic() - started
            stdout = _truncate("".join(execution.logs.stdout))
            stderr = _truncate("".join(execution.logs.stderr))
            ok = execution.error is None
            if execution.error and not stderr:
                # E2B's error carries its own traceback separate from
                # stderr (verified live: a raised exception produces empty
                # logs.stderr but a populated execution.error.traceback) —
                # folded into stderr here so ExecutionResult's shape (and
                # every caller reading .stderr, e.g. SandboxCodeExecutor)
                # doesn't need a separate "or check .error too" path.
                stderr = _truncate(execution.error.traceback or f"{execution.error.name}: {execution.error.value}")

            artifacts: list[str] = []
            artifact_files: dict[str, bytes] = {}
            if ok:
                try:
                    entries = await sbx.files.list(_E2B_HOME_DIR)
                    # FileType is a plain Enum, not a str subclass — verified
                    # live that `entry.type == "file"` is always False (an
                    # easy, silent way to filter out every real file); the
                    # comparison has to be against FileType.FILE itself.
                    artifacts = sorted(
                        e.name for e in entries
                        if e.name not in _E2B_BASELINE_HOME_FILES and e.type == FileType.FILE
                    )
                except Exception as exc:  # noqa: BLE001 - artifact listing is best-effort, never fails the run itself
                    logger.warning("Could not list E2B sandbox files: %s", exc)
                for name in artifacts:
                    if len(artifact_files) >= MAX_ARTIFACT_FILES_CAPTURED:
                        break
                    if not name.lower().endswith(DOWNLOADABLE_ARTIFACT_EXTENSIONS):
                        continue
                    try:
                        content = await sbx.files.read(f"{_E2B_HOME_DIR}/{name}", format="bytes")
                        if len(content) > MAX_ARTIFACT_FILE_BYTES:
                            continue
                        artifact_files[name] = content
                    except Exception as exc:  # noqa: BLE001 - unreadable file shouldn't fail the whole result
                        logger.warning("Could not read E2B artifact %s: %s", name, exc)

            return ExecutionResult(
                status=ExecutionStatus.COMPLETED,
                ok=ok,
                stdout=stdout,
                stderr=stderr,
                returncode=0 if ok else 1,  # E2B reports success/error, not a real process exit code
                artifacts=artifacts[:MAX_ARTIFACTS_LISTED],
                artifact_files=artifact_files,
                duration_seconds=duration,
            )
        finally:
            try:
                await sbx.kill()
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup; a failed kill shouldn't fail the request
                logger.warning("Could not kill E2B sandbox %s: %s", sbx.sandbox_id, exc)


def _truncate(text) -> str:
    if not isinstance(text, str):
        return ""
    return text[:MAX_OUTPUT_CHARS]


def build_sandbox() -> CodeSandbox:
    """Factory mirroring build_provider()/build_embedding_provider() (see
    app/providers.py) — a single seam to swap in a hardened backend later.
    code_execution_mode == "e2b" selects E2BSandbox (a real, isolated
    micro-VM sandbox — see its own docstring); anything else (the default
    "local", or "disabled") returns LocalSubprocessSandbox, whose own run()
    already handles the disabled case by checking code_execution_mode
    itself. E2BSandbox is still constructed even without E2B_SANDBOX set —
    its run() reports that clearly (ExecutionStatus.ERROR) rather than this
    factory needing its own separate "is it configured" branch."""
    if settings.code_execution_mode == "e2b":
        return E2BSandbox(api_key=settings.e2b_sandbox, timeout_seconds=settings.max_code_execution_seconds)
    return LocalSubprocessSandbox()
