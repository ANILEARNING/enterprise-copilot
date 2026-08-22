# Artifacts Skill

## Trigger
"Artifact" names two distinct, unrelated things in this codebase — do not conflate them:

1. **Execution artifacts** — files a sandboxed code run created as a side effect
   (`ExecutionResult.artifacts`, `app/sandbox.py`). Triggered implicitly by the coding skill
   (`coding.md`) whenever approved code writes a file into its own temp workspace.
2. **Generated documents** — a downloadable `.docx`/`.pptx` produced by a skill package
   (`SkillPackageStore`/`SkillRunService`, `app/skills.py`). Triggered by chat-message routing
   (`SkillPackage.chat_triggers`, e.g. "create a document", "generate a pptx") or from the Skills
   tab UI directly.

There is no unified "generate → validate → save to workspace → register artifact" pipeline
covering Markdown/HTML/Python/JSON/CSV output in this app — that description does not match
either real subsystem below. If a task asks for that, it's new work, not an existing capability
to extend; say so rather than assuming it already exists.

## Workflow — execution artifacts
Fully owned by `LocalSubprocessSandbox._run_in_workspace` (`app/sandbox.py`): the set of
filenames present in the workspace after the run, minus the script itself (`snippet.py`), is
diffed against before the run and returned as `artifacts` (capped at `MAX_ARTIFACTS_LISTED`,
20). **Not persisted for download in v1** — the entire temp workspace is deleted
(`shutil.rmtree`) once the result is captured, whether execution succeeded, failed, or timed
out. A run's `artifacts` list is only ever a record of what got created, never a link to
retrieve it later. See `hitl.md` for the approval gate this sits behind.

## Workflow — generated documents
`SkillPackage` (`app/skills.py`) is a self-describing unit: `SKILL.md` front matter declares its
`trigger`/`chat_triggers`, `output` type, and pre-flight `questions` (asked once, HITL-style,
before generation runs — either via the Skills tab's modal form or inline in chat, one question
per turn). Built-in packages live in `skills/` (`docx-generator`, `ppt-generator`); a user can
also upload a `.zip` at runtime (`POST /api/skill-packages/upload`) following the same
`SKILL.md` + `scripts/generate_*.py` + `reference/` shape.

1. **Match**: `SkillPackageStore.select_for_chat` (loose keyword match) or explicit Skills-tab
   invocation.
2. **Answer**: the pre-flight questions, once — via `SkillRunService.start`/`submit_answers`.
3. **Generate**: the package's own `scripts/generate_*.py` entry point runs (e.g.
   `skills/docx-generator/scripts/generate_docx.py`), fed the model-drafted spec plus the user's
   answers — the model drafts structured content (`max_output_tokens_skill_draft`, 2048, since
   the default 256-token budget can't fit a full multi-section JSON document); the script itself
   deterministically renders that spec into a real `.docx`/`.pptx` via `python-docx`/`python-pptx`.
4. **Download**: `POST /api/skill-packages/run/download` serves the generated file. Re-running
   `run/regenerate` with an edited spec produces a fresh file from the same run.

## Constraints
- Execution artifacts and generated documents use **different** underlying mechanisms
  (`CodeSandbox` vs. `SkillPackageStore`) — do not wire one skill's output through the other's
  pipeline.
- Skill-package generation scripts are themselves subprocess-executed with the same scrubbed
  environment (`PATH`/`SYSTEMROOT` only) and the same `MAX_CODE_EXECUTION_SECONDS` timeout as
  `CodeSandbox` (`app/skills.py`'s generation call site mirrors `app/sandbox.py`'s) — keep it
  that way if either changes. Don't relax this discipline for generation scripts just because
  the output is "just a document" rather than arbitrary code.
- A model-drafted spec is still model output — validate/sanitize it the same way any other
  model-generated content headed for a file gets treated, before the generation script consumes
  it.

## Completion
Execution artifacts: named in the execution result, understood as ephemeral — never claimed as
downloadable in v1. Generated documents: a real file the user can download
(`download_ready: true`, a working `POST /api/skill-packages/run/download`) — never a response
that claims a document was created without one actually existing on disk for that run.
