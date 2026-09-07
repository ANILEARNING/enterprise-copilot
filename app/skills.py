"""Skill packages: pluggable generation skills (SKILL.md + scripts/ + reference/),
built-in (checked into `skills/`) or uploaded as a .zip at runtime.

Distinct from `AgentRegistry`/`SkillRegistry` in app/agents.py, which are the
orchestrator's small set of always-available internal capabilities. A
SkillPackage is an external, self-describing unit: it declares its own
pre-flight questions (HITL, answered once before generation runs) and produces
a downloadable file via its own `scripts/generate_*.py` entry point. See
skills/ppt-generator and skills/docx-generator for the two built-in examples,
and their SKILL.md for the front-matter/question schema this module parses.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

import yaml

EventSink = Callable[[dict], Awaitable[None]]


async def _emit(on_event: EventSink | None, event: dict) -> None:
    if on_event is not None:
        await on_event(event)

from .blob_store import BlobStore, BlobStoreError
from .config import settings
from .skill_render import render_spec_as_chat_text

logger = logging.getLogger(__name__)

BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
# Where uploaded (non-builtin) skill packages and skill-run history/output
# persist across restarts — same data/ root and file-backed-cache pattern as
# SessionStore (app/storage.py:DEFAULT_DATA_DIR). Previously both lived only
# in-memory (+ a wiped-on-reboot tempfile.mkdtemp dir for uploaded skills'
# extracted files), so an app restart silently lost every uploaded skill and
# its entire run history — this is the fix.
DEFAULT_SKILLS_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "skills"
DEFAULT_SKILL_RUNS_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "skill-runs"
MAX_OUTPUT_CHARS = 10000

# Real MIME types for skill-run output files, by extension — set on the B2
# object at upload time (see SkillRunService._upload_outputs) so it's
# available for both the B2 object's own metadata and the download route's
# response (app/routes.py:skill_run_download reads it back off the same
# extension, kept in sync with this map deliberately rather than shared
# code, since routes.py can't import from here without a circular import).
_SKILL_OUTPUT_CONTENT_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SkillPackageError(ValueError):
    """Any bad/unparseable skill package or run request. Routes turn this
    into an HTTP 400 — the message is safe to show a client as-is."""


# --- skill package model + SKILL.md parsing ----------------------------------

@dataclass
class SkillQuestion:
    id: str
    prompt: str
    type: str = "text"  # "text" | "select" | "file" | "multiselect"
    options: list[str] = field(default_factory=list)
    required: bool = False
    placeholder: str = ""
    allow_other: bool = False
    # type == "file" only: extensions this question accepts (e.g.
    # ["csv","xlsx"]), or the single sentinel "folder" for a multi-file
    # picker (see docs/skill-architecture.md's file-question section) — the
    # frontend renders this as the file input's `accept` list, and the
    # upload route (POST /skill-packages/run/upload-answer-file) re-validates
    # it server-side, never trusting the client-only check.
    accept: list[str] = field(default_factory=list)
    # Conditional visibility: this question is only shown/required once
    # another question's answer matches. Two shapes:
    #   {"question_id": "sections", "includes": "ROI / Cost-Benefit Analysis"}
    #     — true when a multiselect/text driver's answer (a JSON array
    #     string, or a plain string) contains this value.
    #   {"question_id": "output_format", "equals": "PDF"}
    #     — true when a select/text driver's answer equals this value
    #     exactly (case-insensitive).
    # None (the default) means "always shown" — every existing skill's
    # questions are unaffected. See show_if_met() below and
    # static/app.js's mirrored client-side visibility pass.
    show_if: dict | None = None

    @staticmethod
    def from_dict(data: dict) -> "SkillQuestion":
        show_if = data.get("show_if")
        return SkillQuestion(
            id=str(data.get("id", "")).strip(),
            prompt=str(data.get("prompt", "")).strip(),
            type=(str(data.get("type", "text")).strip() or "text"),
            options=[str(o) for o in (data.get("options") or [])],
            required=bool(data.get("required", False)),
            placeholder=str(data.get("placeholder", "")),
            allow_other=bool(data.get("allow_other", False)),
            accept=[str(a).strip().lower() for a in (data.get("accept") or []) if str(a).strip()],
            show_if=dict(show_if) if isinstance(show_if, dict) else None,
        )


def show_if_met(question: "SkillQuestion", answers: dict[str, str]) -> bool:
    """Evaluates a question's show_if condition (see SkillQuestion.show_if)
    against a submitted-answers dict. True (shown) whenever show_if is None
    — the common case, every existing skill's questions. Never raises on a
    malformed/missing driver — an unmet or broken condition just means
    "not shown," the same as if the question were hidden client-side, so a
    server-side validation bug never blocks an otherwise-valid submission.
    Used by both SkillRunService.submit_answers' required-answer check and
    (mirrored) static/app.js's client-side visibility pass."""
    if not question.show_if:
        return True
    driver_id = question.show_if.get("question_id")
    if not driver_id:
        return True
    driver_value = (answers.get(driver_id) or "").strip()
    if "includes" in question.show_if:
        target = str(question.show_if["includes"])
        try:
            parsed = json.loads(driver_value)
            choices = parsed if isinstance(parsed, list) else [driver_value]
        except json.JSONDecodeError:
            choices = [driver_value]
        return target in [str(c) for c in choices]
    if "equals" in question.show_if:
        target = str(question.show_if["equals"]).strip().lower()
        return driver_value.strip().lower() == target
    return True


@dataclass
class SkillPackage:
    skill_id: str
    name: str
    description: str
    trigger: str
    output: str
    questions: list[SkillQuestion]
    instructions: str  # SKILL.md body (workflow) — fed to the model when drafting a spec
    root_dir: Path
    builtin: bool = False
    # Precise phrases for chat-message routing — see the chat_triggers comment
    # in skills/*/SKILL.md. Deliberately separate from `trigger` (prose) and
    # from select_for_task's loose keyword match (used for the Skills view),
    # since a false-positive match on an ordinary chat message is a bad UX.
    chat_triggers: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "skill_id": self.skill_id, "name": self.name, "description": self.description,
            "output": self.output, "builtin": self.builtin,
            "questions": [q.__dict__ for q in self.questions],
        }


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _parse_skill_md(path: Path, skill_id: str, builtin: bool) -> SkillPackage:
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise SkillPackageError(f"{path.name} is missing YAML front matter (--- ... ---)")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillPackageError(f"{path.name} front matter is not valid YAML: {exc}")
    if not isinstance(meta, dict):
        raise SkillPackageError(f"{path.name} front matter must be a mapping")

    name = str(meta.get("name", "")).strip()
    if not name:
        raise SkillPackageError(f"{path.name} front matter must include a non-empty 'name'")

    questions_raw = meta.get("questions") or []
    if not isinstance(questions_raw, list):
        raise SkillPackageError(f"{path.name} 'questions' must be a list")
    questions = [SkillQuestion.from_dict(q) for q in questions_raw]
    for q in questions:
        if not q.id or not q.prompt:
            raise SkillPackageError(f"{path.name}: every question needs a non-empty 'id' and 'prompt'")

    return SkillPackage(
        skill_id=skill_id,
        name=name,
        description=str(meta.get("description", "")).strip(),
        trigger=str(meta.get("trigger", "")).strip(),
        output=str(meta.get("output", "")).strip().lower(),
        questions=questions,
        instructions=match.group(2).strip(),
        root_dir=path.parent,
        builtin=builtin,
        chat_triggers=[str(t).strip().lower() for t in (meta.get("chat_triggers") or []) if str(t).strip()],
    )


def _find_skill_md(root: Path) -> Path | None:
    for pattern in ("SKILL.md", "skill.md"):
        matches = sorted(root.rglob(pattern))
        if matches:
            return matches[0]
    return None


def _safe_extract(zf: zipfile.ZipFile, target_dir: Path) -> None:
    """Guards against zip-slip (a member path escaping target_dir via '../'
    or an absolute path) before extracting anything, per the project's
    'sanitize uploaded files' rule."""
    resolved_target = target_dir.resolve()
    for member in zf.infolist():
        member_path = (target_dir / member.filename).resolve()
        if resolved_target not in member_path.parents and member_path != resolved_target:
            raise SkillPackageError(f"Unsafe path in skill zip: {member.filename!r}")
    zf.extractall(target_dir)


# --- store ---------------------------------------------------------------------

class SkillPackageStore:
    MAX_ZIP_BYTES = 20 * 1024 * 1024

    def __init__(self, data_dir: Path | None = None):
        self.skills: dict[str, SkillPackage] = {}
        # Uploaded skills' extracted files live here (data/skills/<skill_id>/
        # by default) so they survive a process restart — see
        # _load_uploaded_skills below. Matches SessionStore's
        # data_dir-with-real-default constructor shape (app/storage.py).
        self.data_dir = data_dir or DEFAULT_SKILLS_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._load_builtin_skills()
        self._load_uploaded_skills()

    def _load_builtin_skills(self) -> None:
        if not BUILTIN_SKILLS_DIR.is_dir():
            return
        for entry in sorted(BUILTIN_SKILLS_DIR.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                skill = _parse_skill_md(skill_md, skill_id=entry.name, builtin=True)
            except SkillPackageError as exc:
                logger.warning("Skipping built-in skill %s: %s", entry.name, exc)
                continue
            self.skills[skill.skill_id] = skill

    def _load_uploaded_skills(self) -> None:
        """Reloads every previously-uploaded skill from self.data_dir on
        startup — same idea as _load_builtin_skills, just over a directory of
        skill_id-named folders instead of the checked-in skills/ tree. A
        folder that fails to parse (corrupted/partial extraction) is skipped
        with a warning rather than crashing startup, matching
        _load_builtin_skills' own graceful-skip posture."""
        if not self.data_dir.is_dir():
            return
        for entry in sorted(self.data_dir.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = _find_skill_md(entry)
            if skill_md is None:
                continue
            try:
                skill = _parse_skill_md(skill_md, skill_id=entry.name, builtin=False)
            except SkillPackageError as exc:
                logger.warning("Skipping persisted skill %s: %s", entry.name, exc)
                continue
            self.skills[skill.skill_id] = skill

    def list(self) -> list[dict]:
        return [s.public() for s in self.skills.values()]

    def get(self, skill_id: str) -> SkillPackage:
        if skill_id not in self.skills:
            raise KeyError("Skill not found")
        return self.skills[skill_id]

    def add_from_zip(self, raw: bytes) -> SkillPackage:
        if len(raw) > self.MAX_ZIP_BYTES:
            raise SkillPackageError(f"Skill package exceeds the {self.MAX_ZIP_BYTES // (1024 * 1024)} MB limit.")

        skill_id = str(uuid4())
        # Persistent location (data/skills/<skill_id>/), not a tempfile.mkdtemp
        # dir — the whole point is that this survives a restart (see
        # _load_uploaded_skills). skill_id is our own uuid4, safe as a path
        # component.
        target_dir = self.data_dir / skill_id
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    _safe_extract(zf, target_dir)
            except zipfile.BadZipFile:
                raise SkillPackageError("Uploaded file is not a valid .zip archive.")

            skill_md = _find_skill_md(target_dir)
            if skill_md is None:
                raise SkillPackageError("Zip must contain a SKILL.md file.")

            skill = _parse_skill_md(skill_md, skill_id=skill_id, builtin=False)
        except SkillPackageError:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise

        self.skills[skill.skill_id] = skill
        return skill

    def delete(self, skill_id: str) -> None:
        skill = self.get(skill_id)
        if skill.builtin:
            raise SkillPackageError("Built-in skills can't be deleted.")
        # skill.root_dir is SKILL.md's own parent (_find_skill_md searches
        # recursively, so a zip with one top-level folder nests it one level
        # below the persistent data_dir/<skill_id>/ container) — remove that
        # whole container, not just the nested folder, so no empty directory
        # is left behind in data/skills/ after a delete.
        shutil.rmtree(self.data_dir / skill_id, ignore_errors=True)
        del self.skills[skill_id]

    def select_for_task(self, task: str) -> SkillPackage | None:
        """Keyword match against each skill's name/trigger text, mirroring
        AgentRegistry.select's approach (app/agents.py). Loose on purpose —
        used where a mismatch just means an option in a UI is less relevant,
        not where a false positive would hijack a conversation. See
        select_for_chat for the routing used from live chat messages."""
        lowered = task.lower()
        for skill in self.skills.values():
            haystack = f"{skill.name} {skill.trigger}".lower()
            keywords = set(re.findall(r"[a-z]{4,}", haystack))
            if any(keyword in lowered for keyword in keywords):
                return skill
        return None

    def select_for_chat(self, task: str) -> SkillPackage | None:
        """Phrase match against each skill's curated chat_triggers — used to
        route a plain chat message (e.g. "create a docx about...") into a
        skill run. Deliberately precise (multi-word phrases, or a handful of
        genuinely distinctive single words like "pptx") rather than
        select_for_task's loose single-keyword match, since a false positive
        here silently derails an ordinary conversation into a Q&A form."""
        lowered = task.lower()
        for skill in self.skills.values():
            if any(phrase in lowered for phrase in skill.chat_triggers):
                return skill
        return None


# --- spec drafting (answers -> the JSON each skill's script expects) --------

def _extract_json(text: str) -> dict | None:
    text = text.strip()
    fence_match = re.match(r"^```(?:json)?\s*\n(.*?)\n```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            return None
    return None


_SPEC_META_FIELDS = {"topic", "audience", "tone", "design", "style", "length", "doc_type"}

# Sentinel skill.output value meaning "this skill's script may produce more
# than one file per run, chosen by spec['output_formats']" — see
# run_generation_script below and docs/skill-architecture.md. Every ordinary
# skill (docx-generator: "docx", ppt-generator: "pptx") keeps a plain single
# extension and is completely unaffected.
MULTI_FORMAT_OUTPUT = "docx+pdf"


def _parse_output_formats(answer: str | None) -> list[str]:
    """Maps a `type: "select"` output_format question's answer text (e.g.
    "Word (.docx)", "PDF", "Both") to real extensions. Substring/case-
    insensitive on purpose — a skill author's exact option wording
    ("PDF" vs. "PDF only" vs. ".pdf") shouldn't matter, and defaults to
    docx-only for anything unrecognized/unset so generation never fails
    just because this one question was skipped, same graceful-default
    posture as the rest of this module."""
    normalized = (answer or "").strip().lower()
    if "both" in normalized:
        return ["docx", "pdf"]
    has_pdf = "pdf" in normalized
    has_docx = "word" in normalized or "docx" in normalized
    if has_pdf and has_docx:
        return ["docx", "pdf"]
    if has_pdf:
        return ["pdf"]
    return ["docx"]


def _fallback_brd_spec(answers: dict[str, str]) -> dict:
    """_fallback_spec's branch for skill.output == MULTI_FORMAT_OUTPUT
    (brd-prd-generator) — maps its answers (project_name/objective/
    stakeholders/doc_type/sections + each section's own follow-up question)
    into a minimal-but-real spec matching generate_brd.py's documented
    shape, so the skill works end-to-end with zero credentials configured,
    same guarantee every other skill already has. Only includes a section
    key when that section was actually selected in the `sections` answer."""
    try:
        selected = set(json.loads(answers.get("sections") or "[]"))
    except json.JSONDecodeError:
        selected = set()

    sections: dict = {}
    if "User Stories & Acceptance Criteria" in selected:
        persona_lines = [ln.strip() for ln in (answers.get("user_story_personas") or "").splitlines() if ln.strip()]
        sections["user_stories"] = {"stories": [
            {"persona": line, "goal": "achieve their goal", "benefit": "the project objective is met",
             "acceptance_criteria": [{"given": "the described context", "when": "the persona acts",
                                       "then": "the expected outcome occurs"}]}
            for line in (persona_lines or ["End user"])
        ]}
    if "Process/Workflow Mapping (BPMN-style)" in selected:
        sections["workflow"] = {
            "current_state": [(answers.get("current_state_process") or "Not described.")],
            "future_state": [(answers.get("future_state_process") or "Not described.")],
            "summary": "Draft workflow summary — regenerate with a configured model for full detail.",
        }
    if "SWOT & Gap Analysis" in selected:
        context = answers.get("swot_context") or "Not described."
        sections["swot"] = {
            "strengths": [context], "weaknesses": ["Not yet assessed."],
            "opportunities": ["Not yet assessed."], "threats": ["Not yet assessed."],
            "gap_analysis": "Draft gap analysis — regenerate with a configured model for full detail.",
        }
    if "Use Case Specifications" in selected:
        use_case_lines = [ln.strip() for ln in (answers.get("use_cases") or "").splitlines() if ln.strip()]
        sections["use_cases"] = {"use_cases": [
            {"name": line, "actor": "User", "preconditions": "Not specified.",
             "main_flow": [line], "alternate_flow": "Not specified.", "postconditions": "Not specified."}
            for line in (use_case_lines or ["Primary use case"])
        ]}
    if "Data & Integration Mapping" in selected:
        sections["data_integration"] = {
            "systems": [(answers.get("systems_and_apis") or "Not described.")],
            "data_flow": "Draft data flow — regenerate with a configured model for full detail.",
        }
    if "ROI / Cost-Benefit Analysis" in selected:
        sections["roi"] = {
            "costs": [{"item": "See stakeholder input", "amount": answers.get("roi_inputs") or "Not provided."}],
            "benefits": [], "payback_note": "",
        }
    if "Requirements Traceability Matrix" in selected:
        scope = answers.get("traceability_scope") or "Not described."
        sections["traceability"] = {"rows": [
            {"req_id": "REQ-001", "description": scope, "source": answers.get("stakeholders") or "Stakeholder",
             "test_case_id": "TC-001", "status": "Not Started"},
        ]}

    return {
        "meta": {
            "project_name": answers.get("project_name") or "Untitled Project",
            "doc_type": answers.get("doc_type") or "BRD (Business Requirements Document)",
            "objective": answers.get("objective") or "",
            "stakeholders": answers.get("stakeholders") or "",
        },
        "elicitation_summary": (
            f"This document covers {answers.get('project_name') or 'the project'} for "
            f"{answers.get('stakeholders') or 'the identified stakeholders'}."
        ),
        "output_formats": _parse_output_formats(answers.get("output_format")),
        "sections": sections,
    }


def _fallback_spec(skill: SkillPackage, answers: dict[str, str]) -> dict:
    """Deterministic, LLM-free spec builder — used whenever the model's
    response can't be parsed as JSON (including MockProvider's demo text, so
    the whole skill pipeline still works end-to-end with no credentials
    configured). Maps answers straight onto the script's expected shape
    instead of drafting real content."""
    if skill.output == MULTI_FORMAT_OUTPUT:
        return _fallback_brd_spec(answers)
    topic = answers.get("topic") or skill.name
    other_answers = [v for k, v in answers.items() if k not in _SPEC_META_FIELDS and v]
    base = {"title": topic, "subtitle": answers.get("audience", ""), "tone": answers.get("tone", "")}
    if skill.output == "pptx":
        base["theme"] = answers.get("design", "")
        base["slides"] = [{"title": "Overview", "bullets": other_answers or [topic]}]
    elif skill.output == "docx":
        base["style"] = answers.get("style", "")
        heading = answers.get("doc_type") or "Overview"
        base["sections"] = [{"heading": heading, "paragraphs": other_answers or [f"This document covers: {topic}."]}]
    else:
        base["slides"] = []
    return base


async def draft_spec(skill: SkillPackage, answers: dict[str, str], provider) -> tuple[dict, str, bool]:
    """Returns (spec, provider_name, used_fallback). `used_fallback` is True
    whenever the content wasn't actually authored by the configured model —
    either the provider itself fell back (e.g. Gemini/Ollama unavailable, so
    MockProvider ran) or the model's response couldn't be parsed as JSON, so
    the deterministic `_fallback_spec` template was used instead. Callers use
    this to show the real provider/fallback state instead of guessing."""
    def _answer_for_prompt(q: SkillQuestion) -> str:
        # A file-type answer's value is an internal file_id (see
        # FileAnswerStore) — never meaningful prose, so don't leak the raw id
        # into the prompt; just note that a file was (or wasn't) provided.
        # The generation script gets the real path separately, merged into
        # the spec by SkillRunService.submit_answers after this drafts it
        # (see that method's "uploaded_files" merge) — the model never needs
        # the path itself to write prose describing the deliverable.
        if q.type == "file":
            return "(file uploaded)" if (answers.get(q.id) or "").strip() else "(not provided)"
        if q.type == "multiselect":
            # Stored as a JSON array string (see collectSkillAnswers in
            # static/app.js) — show the model a plain comma-separated list
            # rather than raw JSON syntax.
            raw = answers.get(q.id) or ""
            try:
                choices = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                choices = []
            return ", ".join(str(c) for c in choices) if choices else "(none selected)"
        return answers.get(q.id) or "(not answered)"

    # Questions the user never even saw (an unmet show_if — e.g. the ROI
    # follow-up when ROI wasn't selected) are omitted rather than shown as
    # "(not answered)", so the model isn't confused into thinking something
    # relevant was skipped.
    visible_questions = [q for q in skill.questions if show_if_met(q, answers)]
    prompt = (
        f"{skill.instructions}\n\n"
        "The user answered this skill's pre-flight questions:\n"
        + "\n".join(f"- {q.prompt}: {_answer_for_prompt(q)}" for q in visible_questions)
        + "\n\nRespond with ONLY a single JSON object matching the spec shape documented in "
          "the Workflow section above (the exact shape scripts/generate_*.py expects) — no "
          "prose, no markdown code fence, just the raw JSON object."
    )
    result = await provider.complete(
        prompt, [], max_tokens=settings.max_output_tokens_skill_draft, json_mode=True,
    )
    spec = _extract_json(result.text)
    used_fallback = result.used_fallback
    if spec is None:
        spec = _fallback_spec(skill, answers)
        used_fallback = True
    return spec, result.provider, used_fallback


# --- running a skill's generation script -------------------------------------

def _find_generation_script(skill: SkillPackage) -> Path:
    scripts_dir = skill.root_dir / "scripts"
    matches = sorted(scripts_dir.glob("generate_*.py")) if scripts_dir.is_dir() else []
    if not matches:
        raise SkillPackageError(f"Skill {skill.name!r} has no scripts/generate_*.py entry point.")
    return matches[0]


def run_generation_script(skill: SkillPackage, spec: dict, output_dir: Path | None = None) -> list[Path]:
    """Runs the skill's generation script as a subprocess, same safety
    posture as LocalSubprocessSandbox (app/sandbox.py): scrubbed env (no .env/secrets
    passthrough), timeout, output cap. Not a secure sandbox — development-only,
    per the project's security rules.

    `output_dir`: where the generated file(s) land. Defaults to a fresh
    tempfile.mkdtemp dir (the original behavior, still used by any caller
    that doesn't care about the file surviving a restart); SkillRunService
    passes its own persistent data_dir/<run_id>/ instead so get_output()
    still resolves after a reboot (see SkillRunService._write).

    Returns a list of every file the script actually produced — one element
    for an ordinary single-format skill (unchanged behavior, just wrapped in
    a list), or one element per requested format for a skill.output ==
    MULTI_FORMAT_OUTPUT skill (see that constant's docstring). The script is
    invoked exactly once either way — for a multi-format skill it reads
    spec['output_formats'] itself and writes each requested file next to the
    given --output base path (e.g. skills/brd-prd-generator/scripts/
    generate_brd.py); this function does not re-invoke the script per
    format."""
    if settings.code_execution_mode != "local":
        raise SkillPackageError("Skill generation is disabled in this environment.")

    script_path = _find_generation_script(skill)
    output_dir = output_dir or Path(tempfile.mkdtemp(prefix="copilot-skill-output-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    multi_format = skill.output == MULTI_FORMAT_OUTPUT
    # A multi-format script decides its own real extension(s) per
    # spec['output_formats'] and writes "<output_base>.<ext>" itself, so the
    # base path handed to it deliberately carries no extension. An ordinary
    # skill keeps today's exact "output.<ext>" path.
    output_path = output_dir / ("output" if multi_format else f"output.{skill.output or 'bin'}")

    safe_env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path), "--output", str(output_path)],
            input=json.dumps(spec),
            capture_output=True,
            text=True,
            timeout=settings.max_code_execution_seconds,
            cwd=str(skill.root_dir),
            env=safe_env,
        )
    except subprocess.TimeoutExpired:
        raise SkillPackageError(f"Generation exceeded {settings.max_code_execution_seconds}s timeout.")

    if not multi_format:
        if proc.returncode != 0 or not output_path.exists():
            detail = (proc.stderr or proc.stdout or "unknown error")[:MAX_OUTPUT_CHARS]
            raise SkillPackageError(f"Generation failed: {detail}")
        return [output_path]

    # Multi-format: the script wrote zero or more "<output_path>.<ext>"
    # files directly — collect whichever real files exist rather than
    # trusting the subprocess's exit code alone (a script that wrote one of
    # two requested formats before hitting a partial error should still
    # hand back what it did produce, same "don't lose real work" posture as
    # every other generation path in this app).
    produced = sorted(
        p for p in output_dir.iterdir()
        if p.is_file() and p.stem == output_path.name and p.suffix
    )
    if not produced:
        detail = (proc.stderr or proc.stdout or "unknown error")[:MAX_OUTPUT_CHARS]
        raise SkillPackageError(f"Generation failed: {detail}")
    return produced


# --- file-question answers: uploaded files staged by reference -------------

class FileAnswerStore:
    """Backs `type: "file"` skill questions (see SkillQuestion.accept):
    an uploaded answer's bytes are staged here, on disk, and the answers
    dict passed around the rest of this module only ever carries the
    returned file_id — never raw bytes inline (per the confirmed by-
    reference design). Colocated under the owning run's own persistent
    directory (data/skill-runs/<run_id>/answers/) since a file answer only
    ever makes sense attached to its run, and survives a restart the same
    way the run record itself does.

    Deliberately NOT app/artifacts.py's ArtifactStore: that store is
    documented in-memory-only bytes-in-a-dict (.claude/rules/architecture.md
    posture) and never touches disk, whereas a generation script needs a
    real filesystem path to open, and the upload must survive a restart.
    """

    def __init__(self, run_runs_dir: Path):
        self._run_runs_dir = run_runs_dir

    def save(self, run_id: str, filename: str, raw: bytes) -> tuple[str, Path]:
        file_id = str(uuid4())
        # One subdirectory per upload, named by file_id, with the original
        # (already-sanitized) filename preserved as the actual filename
        # inside it — avoids parsing the real name back out of a combined
        # "id-filename" string later (file_id is itself a UUID full of
        # hyphens, which made that parse ambiguous/wrong).
        file_dir = self._run_runs_dir / run_id / "answers" / file_id
        file_dir.mkdir(parents=True, exist_ok=True)
        path = file_dir / filename
        path.write_bytes(raw)
        return file_id, path

    def resolve(self, run_id: str, file_id: str) -> Path:
        """Finds a previously-saved file by id. No separate index file is
        kept — the filesystem itself is the source of truth (same posture
        SessionStore takes for session JSON files: if the expected file
        exists, it's valid), since file_id is already the directory name
        this store wrote the upload under."""
        file_dir = self._run_runs_dir / run_id / "answers" / file_id
        if file_dir.is_dir():
            candidates = [p for p in file_dir.iterdir() if p.is_file()]
            if candidates:
                return candidates[0]
        raise KeyError("Uploaded file not found")


# --- run session (the HITL state machine: ask -> answer -> generate) --------

@dataclass
class SkillRunSession:
    run_id: str
    skill_id: str
    status: str  # AWAITING_ANSWERS -> GENERATING -> COMPLETED | COMPLETED_INLINE | FAILED
    answers: dict[str, str] = field(default_factory=dict)
    spec: dict | None = None  # the drafted content (title/slides or sections) — viewable & editable
    # B2 object keys for every file this run's generation actually produced
    # — one element for an ordinary skill (docx-generator, ppt-generator),
    # one per requested format for a MULTI_FORMAT_OUTPUT skill (see
    # run_generation_script). Empty until generation completes. Each key
    # ends in the file's real extension (see SkillRunService._upload_outputs)
    # so it doubles as this run's own little content-addressed record of
    # what format each file is, without a separate lookup.
    output_keys: list[str] = field(default_factory=list)
    error: str | None = None
    # Set once draft_spec() runs (on the finish turn): which provider actually
    # drafted `spec`, and whether it's real model output or fallback template
    # content. None/False while a run is still collecting answers.
    provider: str | None = None
    used_fallback: bool = False
    # Set only when status is COMPLETED_INLINE — the spec rendered as chat
    # text (app/skill_render.py) instead of a generated file. See
    # submit_answers' `delivery` param. None on every other status, including
    # a plain COMPLETED (a real file) run.
    rendered_text: str | None = None
    created_at: str = field(default_factory=_now_iso)

    def public(self) -> dict:
        return {
            "run_id": self.run_id, "skill_id": self.skill_id, "status": self.status,
            "answers": self.answers, "spec": self.spec, "error": self.error,
            "provider": self.provider, "used_fallback": self.used_fallback,
            "download_ready": self.status == "COMPLETED",
            "rendered_text": self.rendered_text,
            # Real file extensions actually produced (e.g. ["docx", "pdf"])
            # — lets the frontend render one Download button per format
            # without a separate call. Derived from each output_key's own
            # extension (see class docstring), not a static skill.output
            # string, so it's always accurate.
            "outputs": [k.rsplit(".", 1)[-1] for k in self.output_keys if "." in k],
        }

    def to_disk(self) -> dict:
        """Full on-disk record (superset of public()) — includes
        output_keys, which public()/the API response never expose directly
        (each file is only ever served through the dedicated download
        route, which fetches it from B2 by key), but which
        SkillRunService._load needs to resolve get_output()/list_output_keys()
        after a restart."""
        data = self.public()
        data["output_keys"] = list(self.output_keys)
        data["created_at"] = self.created_at
        return data

    @staticmethod
    def from_disk(data: dict) -> "SkillRunSession":
        return SkillRunSession(
            run_id=data["run_id"], skill_id=data["skill_id"], status=data["status"],
            answers=data.get("answers") or {}, spec=data.get("spec"),
            output_keys=list(data.get("output_keys") or []),
            error=data.get("error"), provider=data.get("provider"),
            used_fallback=bool(data.get("used_fallback", False)),
            rendered_text=data.get("rendered_text"),
            created_at=data.get("created_at") or _now_iso(),
        )


class SkillRunService:
    """Orchestrates one skill run: start() opens it awaiting answers (the
    pre-flight HITL form — see skill.questions), submit_answers() validates
    required answers, drafts a generation spec, runs the skill's script, and
    lands the run in COMPLETED/FAILED. get_output() hands back the
    downloadable file for a completed run.

    Run records persist to disk (data/skill-runs/<run_id>/), same
    file-backed + in-memory-cache + write-through pattern as SessionStore
    used to (app/storage.py, before it moved to Redis — see
    app/session_store.py). Generated output files themselves live in B2
    (see _upload_outputs) — a generation script still writes locally first
    (subprocess -> --output <path> is unavoidably a local-filesystem
    handoff, see run_generation_script), but that local copy is uploaded to
    B2 and discarded immediately after, never kept as a second persistent
    copy alongside B2.
    """

    def __init__(
        self, skill_store: SkillPackageStore, provider, data_dir: Path | None = None,
        blob_store: BlobStore | None = None,
    ):
        self.skill_store = skill_store
        self.provider = provider
        self.runs: dict[str, SkillRunSession] = {}
        self.data_dir = data_dir or DEFAULT_SKILL_RUNS_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.files = FileAnswerStore(self.data_dir)
        # blob_store lets tests inject a fake without real B2 credentials —
        # same seam as app/document_store.py's blob_store param. Built
        # lazily from settings (see _blobs), not here, so constructing a
        # SkillRunService never itself requires B2_* to be configured.
        self._blob_store = blob_store
        self._load_runs()

    def _blobs(self) -> BlobStore:
        if self._blob_store is None:
            self._blob_store = BlobStore(
                endpoint=settings.b2_endpoint, bucket=settings.b2_bucket_name,
                key_id=settings.b2_key_id, application_key=settings.b2_application_key,
            )
        return self._blob_store

    def _run_dir(self, run_id: str) -> Path:
        return self.data_dir / run_id

    def _record_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "run.json"

    def _fresh_output_dir(self, run_id: str) -> Path:
        # A throwaway local staging directory for run_generation_script's
        # subprocess to write into — a real filesystem path is unavoidable
        # here (the script is invoked with --output <path>), but nothing
        # under it survives past _upload_outputs: every file it contains is
        # uploaded to B2 and the whole directory is then removed, unlike the
        # old behavior of keeping this as the run's permanent storage.
        return Path(tempfile.mkdtemp(prefix=f"copilot-skill-output-{run_id}-"))

    def _upload_outputs(self, run_id: str, local_paths: list[Path]) -> list[str]:
        """Uploads every locally-generated output file to B2 and returns
        their object keys, in the same order as local_paths. Each key ends
        in the file's real extension (matches local_paths[i].suffix) so
        SkillRunSession.public()'s `outputs` list and get_output()'s
        format-matching both work directly off the key string, no separate
        extension record needed. The local staging directory (this run's
        _fresh_output_dir) is removed once every file is uploaded — nothing
        from it is kept, matching class docstring's "never a second
        persistent copy alongside B2"."""
        keys = []
        try:
            for path in local_paths:
                key = f"skill-runs/{run_id}/{uuid4()}{path.suffix}"
                self._blobs().put_bytes(key, path.read_bytes(), content_type=_SKILL_OUTPUT_CONTENT_TYPES.get(
                    path.suffix.lstrip("."), "application/octet-stream",
                ))
                keys.append(key)
        finally:
            if local_paths:
                shutil.rmtree(local_paths[0].parent, ignore_errors=True)
        return keys

    def _write(self, run: SkillRunSession) -> None:
        try:
            run_dir = self._run_dir(run.run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            self._record_path(run.run_id).write_text(json.dumps(run.to_disk(), indent=2), encoding="utf-8")
        except OSError as exc:
            # A disk-write failure shouldn't take the request down — the
            # in-memory copy still has this run, it just won't survive a
            # restart, same posture as SessionStore._write.
            logger.warning("Could not persist skill run %s: %s", run.run_id, exc)
        self.runs[run.run_id] = run

    def _load_runs(self) -> None:
        if not self.data_dir.is_dir():
            return
        for entry in sorted(self.data_dir.iterdir()):
            record_path = entry / "run.json" if entry.is_dir() else None
            if not record_path or not record_path.is_file():
                continue
            try:
                data = json.loads(record_path.read_text(encoding="utf-8"))
                run = SkillRunSession.from_disk(data)
            except (json.JSONDecodeError, OSError, KeyError) as exc:
                logger.warning("Could not read skill run file %s: %s", record_path, exc)
                continue
            self.runs[run.run_id] = run

    def start(self, skill_id: str) -> SkillRunSession:
        self.skill_store.get(skill_id)  # raises KeyError if unknown -> 404 at the route
        run = SkillRunSession(run_id=str(uuid4()), skill_id=skill_id, status="AWAITING_ANSWERS")
        self._write(run)
        return run

    def get(self, run_id: str) -> SkillRunSession:
        if run_id not in self.runs:
            raise KeyError("Skill run not found")
        return self.runs[run_id]

    def list(self) -> list[dict]:
        return [r.public() for r in self.runs.values()]

    def upload_answer_file(self, run_id: str, question_id: str, filename: str, raw: bytes) -> dict:
        """Stages one uploaded file for a `type: "file"` question — called by
        the frontend the moment a file is chosen, before the rest of the form
        is submitted (matches the existing "upload happens, then the answer
        references it" pattern already used for skill-zip/doc uploads).
        Returns {file_id, filename, size_bytes}; the frontend puts file_id
        straight into the plain answers dict for this question_id."""
        run = self.get(run_id)
        if run.status != "AWAITING_ANSWERS":
            raise SkillPackageError(f"Run is {run.status}, not awaiting answers.")
        skill = self.skill_store.get(run.skill_id)
        question = next((q for q in skill.questions if q.id == question_id), None)
        if question is None or question.type != "file":
            raise SkillPackageError(f"{question_id!r} is not a file question on this skill.")
        if question.accept and "folder" not in question.accept:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in question.accept:
                raise SkillPackageError(
                    f"{filename!r} isn't accepted here — allowed types: {', '.join(question.accept)}."
                )
        file_id, path = self.files.save(run_id, filename, raw)
        return {"file_id": file_id, "filename": filename, "size_bytes": path.stat().st_size}

    async def submit_answers(
        self, run_id: str, answers: dict[str, str], on_event: EventSink | None = None,
        delivery: str = "file",
    ) -> SkillRunSession:
        """`on_event`, when given, receives live progress for the two real
        steps below — drafting (an LLM call) and generating (an actual
        subprocess running the skill's scripts/generate_*.py) — so a
        streaming UI can show what's actually happening instead of one
        opaque wait. See CopilotService.chat_stream.

        `delivery`: "file" (default — every existing caller, including the
        Skills tab's own pre-flight form, which has no concept of this at
        all) generates and uploads the real file, same as always. "inline"
        (only ever passed by CopilotService's chat-integrated skill Q&A, per
        TurnPlan.delivery — see app/agents.py) renders the drafted spec as
        chat text via app/skill_render.py instead, skipping generation and
        upload entirely, UNLESS this skill has no renderer yet, in which case
        it falls back to "file" exactly as if delivery had been "file" all
        along."""
        run = self.get(run_id)
        if run.status != "AWAITING_ANSWERS":
            raise SkillPackageError(f"Run is {run.status}, not awaiting answers.")
        skill = self.skill_store.get(run.skill_id)

        # A required question hidden by an unmet show_if (e.g. the ROI
        # section's follow-up when "ROI" wasn't checked in the driving
        # multiselect) must not block submission — skip it the same way the
        # frontend never rendered/required it (see show_if_met, mirrored in
        # static/app.js's collectSkillAnswers).
        missing = [
            q.prompt for q in skill.questions
            if q.required and show_if_met(q, answers) and not (answers.get(q.id) or "").strip()
        ]
        if missing:
            raise SkillPackageError("Missing required answers: " + "; ".join(missing))

        # File-type answers arrive as a file_id (uploaded earlier via
        # upload_answer_file) — resolve each to its real staged path now, so
        # a bad/expired id fails the run up front rather than mid-generation.
        # Folder questions (accept: ["folder"]) carry a JSON array of
        # file_ids in the single answer slot (see FileAnswerStore/frontend);
        # everything else is one file_id per question.
        uploaded_files: dict[str, dict] = {}
        for q in skill.questions:
            if q.type != "file":
                continue
            value = (answers.get(q.id) or "").strip()
            if not value:
                continue
            if "folder" in q.accept:
                try:
                    file_ids = json.loads(value)
                except json.JSONDecodeError:
                    raise SkillPackageError(f"Uploaded files for {q.prompt!r} are missing or expired.")
            else:
                file_ids = [value]
            resolved = []
            for file_id in file_ids:
                try:
                    path = self.files.resolve(run_id, file_id)
                except KeyError:
                    raise SkillPackageError(f"Uploaded file for {q.prompt!r} is missing or expired.")
                resolved.append({"filename": path.name, "path": str(path)})
            uploaded_files[q.id] = resolved if "folder" in q.accept else resolved[0]

        run.answers = {k: str(v) for k, v in answers.items()}
        run.status = "GENERATING"
        self._write(run)
        try:
            await _emit(on_event, {
                "stage": "drafting", "label": f"Drafting {skill.output} content…",
            })
            spec, provider_name, used_fallback = await draft_spec(skill, run.answers, self.provider)
            if uploaded_files:
                # Merged in AFTER drafting, not sent through the LLM prompt
                # (draft_spec shows the model a filename placeholder for file
                # questions, never a raw path) — the generation script reads
                # this key directly, same JSON-over-stdin channel it already
                # uses for the rest of the spec.
                spec["uploaded_files"] = uploaded_files
            if skill.output == MULTI_FORMAT_OUTPUT:
                # A MULTI_FORMAT_OUTPUT skill's own script decides which
                # real file(s) to write from spec['output_formats'] — merged
                # in after drafting, same reasoning as uploaded_files above
                # (the model doesn't need to know the output format to write
                # the content). "output_format" is this skill's own
                # question id (see brd-prd-generator/SKILL.md); an unset/
                # unrecognized answer defaults to docx-only rather than
                # failing generation outright.
                spec["output_formats"] = _parse_output_formats(run.answers.get("output_format"))
            run.spec = spec
            run.provider = provider_name
            run.used_fallback = used_fallback

            rendered_text = render_spec_as_chat_text(skill.skill_id, spec) if delivery == "inline" else None
            if rendered_text is not None:
                # Rendered inline — no generation, no upload, no file at all.
                run.rendered_text = rendered_text
                run.status = "COMPLETED_INLINE"
                await _emit(on_event, {"stage": "done_inline", "label": "Rendered inline."})
            else:
                # delivery == "file", or "inline" was requested but this
                # skill has no renderer yet (see render_spec_as_chat_text's
                # docstring) — either way, generate the real file exactly as
                # before this parameter existed.
                await _emit(on_event, {
                    "stage": "executing", "label": f"Running {skill.name} generator…",
                    "script": _find_generation_script(skill).name,
                })
                # Generated to a throwaway local staging dir, then uploaded to
                # B2 and the local copy discarded — see _fresh_output_dir/
                # _upload_outputs' docstrings and class docstring.
                local_paths = run_generation_script(skill, spec, output_dir=self._fresh_output_dir(run_id))
                run.output_keys = self._upload_outputs(run_id, local_paths)
                run.status = "COMPLETED"
                await _emit(on_event, {
                    "stage": "done",
                    "label": f"Saved {', '.join(p.name for p in local_paths)}.",
                    "output_keys": run.output_keys,
                })
        except SkillPackageError as exc:
            run.status = "FAILED"
            run.error = str(exc)
            await _emit(on_event, {"stage": "failed", "label": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a run failure must land in FAILED, not crash the request
            logger.warning("Skill run %s failed unexpectedly: %s", run_id, exc)
            run.status = "FAILED"
            run.error = "Generation failed unexpectedly."
            await _emit(on_event, {"stage": "failed", "label": run.error})
        self._write(run)
        return run

    def regenerate(self, run_id: str, spec: dict) -> SkillRunSession:
        """Re-runs generation from a user-edited spec (the View/Edit flow) —
        skips drafting, since the content is already exactly what the user
        wants. Only valid once a run has produced (or attempted) output."""
        run = self.get(run_id)
        if run.status not in ("COMPLETED", "FAILED"):
            raise SkillPackageError(f"Run is {run.status}; wait for it to finish before editing.")
        skill = self.skill_store.get(run.skill_id)

        run.spec = spec
        run.status = "GENERATING"
        try:
            local_paths = run_generation_script(skill, spec, output_dir=self._fresh_output_dir(run_id))
            run.output_keys = self._upload_outputs(run_id, local_paths)
            run.status = "COMPLETED"
            run.error = None
        except SkillPackageError as exc:
            run.status = "FAILED"
            run.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a run failure must land in FAILED, not crash the request
            logger.warning("Skill run %s regeneration failed unexpectedly: %s", run_id, exc)
            run.status = "FAILED"
            run.error = "Regeneration failed unexpectedly."
        self._write(run)
        return run

    def list_output_keys(self, run_id: str) -> list[tuple[str, str]]:
        """Every completed file for this run, each paired with its
        download filename (skill.name.ext, one entry per real B2 key in
        output_keys — extension taken from the key itself, not a static
        skill.output string, so it's correct for both an ordinary
        single-format skill and a MULTI_FORMAT_OUTPUT one). Returns keys,
        not bytes — see get_output for the one that actually fetches
        content, so listing formats (e.g. to validate a `format` request)
        never pays for a B2 GET it doesn't need."""
        run = self.get(run_id)
        if run.status != "COMPLETED" or not run.output_keys:
            raise SkillPackageError("This run has no completed output yet.")
        skill = self.skill_store.get(run.skill_id)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", skill.name).strip("-") or "output"
        return [(key, f"{safe_name}.{key.rsplit('.', 1)[-1] if '.' in key else 'bin'}") for key in run.output_keys]

    def get_output(self, run_id: str, format: str | None = None) -> tuple[bytes, str, str]:
        """Fetches one output file's bytes from B2. Returns (content,
        filename, content_type). format=None (the default, every existing
        single-output skill's call shape unchanged) picks the sole output;
        for a multi-output run, format="docx"/"pdf" picks the matching file
        by its real extension."""
        outputs = self.list_output_keys(run_id)
        if format is None:
            key, filename = outputs[0]
        else:
            wanted = format.strip().lower().lstrip(".")
            match = next(((k, f) for k, f in outputs if k.rsplit(".", 1)[-1].lower() == wanted), None)
            if match is None:
                raise SkillPackageError(f"This run has no {format!r} output.")
            key, filename = match
        try:
            content = self._blobs().get_bytes(key)
        except KeyError:
            raise SkillPackageError("This run's output file is missing from storage.") from None
        except BlobStoreError as exc:
            raise SkillPackageError(f"Could not fetch output from storage: {exc}") from exc
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        content_type = _SKILL_OUTPUT_CONTENT_TYPES.get(extension, "application/octet-stream")
        return content, filename, content_type
