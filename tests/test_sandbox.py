import pytest

from app.config import settings
from app.sandbox import ExecutionStatus, LocalSubprocessSandbox, build_sandbox


def test_build_sandbox_returns_local_subprocess_sandbox():
    assert isinstance(build_sandbox(), LocalSubprocessSandbox)


# --- successful execution -----------------------------------------------------

def test_successful_execution_reports_completed_ok_and_stdout():
    result = LocalSubprocessSandbox().run("print('hello from sandbox')")
    assert result.status == ExecutionStatus.COMPLETED
    assert result.ok is True
    assert result.returncode == 0
    assert "hello from sandbox" in result.stdout
    assert result.stderr == ""
    assert result.duration_seconds is not None and result.duration_seconds >= 0


# --- invalid code / script failure ---------------------------------------------

def test_syntax_error_is_captured_not_raised():
    result = LocalSubprocessSandbox().run("def broken(:\n    pass")
    assert result.status == ExecutionStatus.COMPLETED  # the sandbox itself ran fine
    assert result.ok is False
    assert result.returncode != 0
    assert "SyntaxError" in result.stderr


def test_runtime_exception_is_captured_not_raised():
    result = LocalSubprocessSandbox().run("raise ValueError('boom')")
    assert result.status == ExecutionStatus.COMPLETED
    assert result.ok is False
    assert result.returncode != 0
    assert "ValueError" in result.stderr
    assert "boom" in result.stderr


def test_output_is_truncated_to_cap():
    result = LocalSubprocessSandbox().run("print('x' * 50000)")
    assert result.ok is True
    assert len(result.stdout) <= 10000


# --- timeout -------------------------------------------------------------------

def test_timeout_is_reported_not_hung(monkeypatch):
    monkeypatch.setattr(settings, "max_code_execution_seconds", 1)
    result = LocalSubprocessSandbox().run("import time; time.sleep(5)")
    assert result.status == ExecutionStatus.TIMEOUT
    assert result.ok is False
    assert "timeout" in (result.error or "").lower()


# --- disabled mode ---------------------------------------------------------------

def test_disabled_mode_refuses_without_running(monkeypatch):
    monkeypatch.setattr(settings, "code_execution_mode", "disabled")
    result = LocalSubprocessSandbox().run("print('should never run')")
    assert result.status == ExecutionStatus.DISABLED
    assert result.ok is False
    assert result.stdout == ""


# --- best-effort static guard (file access restriction) ------------------------

def test_env_access_is_blocked_before_running():
    result = LocalSubprocessSandbox().run("import os\nprint(os.environ.get('SECRET'))")
    assert result.status == ExecutionStatus.BLOCKED
    assert result.ok is False
    assert result.stdout == ""  # never actually ran


def test_absolute_path_open_is_blocked_before_running():
    result = LocalSubprocessSandbox().run("open('C:\\\\Windows\\\\System32\\\\drivers\\\\etc\\\\hosts')")
    assert result.status == ExecutionStatus.BLOCKED
    assert result.stdout == ""


def test_relative_path_open_is_not_blocked():
    # only absolute paths trip the guard -- a script writing inside its own
    # workspace via a relative path is the whole point of "generated artifacts".
    result = LocalSubprocessSandbox().run("open('note.txt', 'w').write('hi')")
    assert result.status == ExecutionStatus.COMPLETED
    assert result.ok is True


# --- artifact tracking -----------------------------------------------------------

def test_generated_artifact_is_tracked_by_name():
    code = "with open('report.csv', 'w') as f:\n    f.write('a,b\\n1,2\\n')"
    result = LocalSubprocessSandbox().run(code)
    assert result.ok is True
    assert "report.csv" in result.artifacts


def test_no_artifacts_when_script_writes_nothing():
    result = LocalSubprocessSandbox().run("print('no files here')")
    assert result.artifacts == []


# --- downloadable artifact capture (dashboards/reports) -------------------------

def test_html_artifact_content_is_captured():
    code = "open('output.html', 'w', encoding='utf-8').write('<html><body>Hi</body></html>')"
    result = LocalSubprocessSandbox().run(code)
    assert result.ok is True
    assert "output.html" in result.artifacts
    assert result.artifact_files == {"output.html": b"<html><body>Hi</body></html>"}


def test_non_downloadable_extension_is_listed_but_not_captured():
    # report.csv still shows up in `artifacts` (any file the script wrote)
    # but its content is never read into artifact_files — only .html/.htm/.pdf
    # qualify (app/sandbox.py:DOWNLOADABLE_ARTIFACT_EXTENSIONS).
    code = "open('report.csv', 'w').write('a,b\\n1,2\\n')"
    result = LocalSubprocessSandbox().run(code)
    assert "report.csv" in result.artifacts
    assert result.artifact_files == {}


def test_artifact_content_not_captured_when_script_fails():
    # A failed run's half-written file isn't worth keeping — see
    # _run_in_workspace's `if proc.returncode == 0` guard.
    code = "open('output.html', 'w').write('<html>')\nraise ValueError('boom')"
    result = LocalSubprocessSandbox().run(code)
    assert result.ok is False
    assert "output.html" in result.artifacts  # still listed
    assert result.artifact_files == {}  # but not captured


# --- HitlService integration: approval gates execution, result is captured -----
#
# HitlService(sandbox) below (no explicit data_dir=) now persists to disk at
# a FIXED default path (settings.data_dir/hitl-requests when configured,
# else the real repo data/ — see app/services.py, same pattern as
# SessionStore.DEFAULT_DATA_DIR) so the real singleton keeps using the same
# directory across a restart. This autouse fixture redirects settings.data_dir
# to a fresh tmp_path per test instead, so this file's several bare
# constructions don't share one real directory across test runs — same
# isolation tests/test_storage.py's SessionStore(data_dir=tmp_path) gets.

@pytest.fixture(autouse=True)
def _isolate_hitl_requests(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    yield


@pytest.mark.asyncio
async def test_hitl_public_result_shape_matches_execution_result():
    from app.services import HitlService
    hitl = HitlService(LocalSubprocessSandbox())
    submitted = hitl.submit_code_execution("print('via hitl')", session_id=None)
    assert submitted["status"] == "WAITING_FOR_APPROVAL"
    assert submitted["result"] is None

    # Execution now runs through a real CodeExecutorAgent (app/hitl_agents.py)
    # instead of a bare sandbox.run() call -- the public result shape (and
    # the fact it's real sandbox output) must still be unchanged.
    decided = await hitl.decide(submitted["request_id"], approved=True)
    assert decided["status"] == "COMPLETED"  # HITL workflow status, distinct from execution status
    assert decided["result"]["status"] == "completed"
    assert decided["result"]["ok"] is True
    assert "via hitl" in decided["result"]["stdout"]
    assert decided["downloadable_artifacts"] == []  # no .html/.pdf produced this time


@pytest.mark.asyncio
async def test_hitl_decide_persists_generated_html_as_downloadable_artifact():
    from app.artifacts import ArtifactStore
    from app.services import HitlService

    store = ArtifactStore()
    hitl = HitlService(LocalSubprocessSandbox(), artifacts=store)
    code = "open('output.html', 'w', encoding='utf-8').write('<html><body>Dashboard</body></html>')"
    submitted = hitl.submit_code_execution(code, session_id="s1")
    decided = await hitl.decide(submitted["request_id"], approved=True)

    assert decided["status"] == "COMPLETED"
    assert len(decided["downloadable_artifacts"]) == 1
    artifact_public = decided["downloadable_artifacts"][0]
    assert artifact_public["filename"] == "output.html"
    assert artifact_public["mime_type"] == "text/html"
    assert artifact_public["view_url"].startswith("/artifacts/")

    # Actually retrievable from the store by the id in that public dict —
    # not just an echoed filename with nothing real behind it.
    stored = store.get(artifact_public["artifact_id"])
    assert stored.content == b"<html><body>Dashboard</body></html>"
    assert stored.session_id == "s1"
    assert stored.hitl_request_id == submitted["request_id"]


@pytest.mark.asyncio
async def test_hitl_decide_without_artifact_store_still_works():
    # HitlService(sandbox) with no explicit artifacts= (the constructor's
    # default) must still function — covers every existing test/call site
    # that predates this feature.
    from app.services import HitlService
    hitl = HitlService(LocalSubprocessSandbox())
    submitted = hitl.submit_code_execution("print('ok')", session_id=None)
    decided = await hitl.decide(submitted["request_id"], approved=True)
    assert decided["status"] == "COMPLETED"
    assert decided["downloadable_artifacts"] == []


@pytest.mark.asyncio
async def test_hitl_rejected_request_never_calls_sandbox():
    from app.services import HitlService

    class ExplodingSandbox:
        def run(self, code):
            raise AssertionError("sandbox.run must not be called for a rejected request")

    hitl = HitlService(ExplodingSandbox())
    submitted = hitl.submit_code_execution("print('should not run')", session_id=None)
    decided = await hitl.decide(submitted["request_id"], approved=False)
    assert decided["status"] == "REJECTED"
    assert decided["result"] is None


@pytest.mark.asyncio
async def test_hitl_await_decision_resolves_when_decided():
    # A live agent-mode turn (see app/hitl_agents.py:await_human_decision)
    # awaits exactly this Future -- decide() must resolve it with the full
    # decided record, result included.
    from app.services import HitlService
    hitl = HitlService(LocalSubprocessSandbox())
    submitted = hitl.submit_code_execution("print('live wait')", session_id=None)
    future = hitl.await_decision(submitted["request_id"])
    assert not future.done()

    decided = await hitl.decide(submitted["request_id"], approved=True)
    assert future.done()
    resolved = await future
    assert resolved["request_id"] == decided["request_id"]
    assert resolved["status"] == "COMPLETED"
    assert "live wait" in resolved["result"]["stdout"]
