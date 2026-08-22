"""Session-wide pytest setup — runs before any test module is imported
(pytest guarantees a root conftest.py loads ahead of test collection), which
matters here specifically: app/services.py builds one real CopilotService()
singleton (`service = CopilotService()`) the moment app.services or app.main
is imported by any test file, and CopilotService's file-backed stores
(SessionStore, SkillPackageStore, SkillRunService — app/services.py) default
to this repo's real data/ directory unless redirected first.

Setting DATA_DIR here, before that import can happen, redirects the whole
data/ tree to a per-test-session temp directory (auto-cleaned by pytest's
own tmp_path_factory machinery) — so running the suite never writes real
session/skill/skill-run files into this repo's own data/ folder. Individual
tests that want their OWN isolated store (not sharing the one process-wide
singleton) should still pass an explicit data_dir=tmp_path, same as
tests/test_storage.py and tests/test_skills.py already do — this fixture
only covers the module-level `service` singleton other tests import.

Also forcing AI_MODE=mock here for the same reason: build_ai_provider(),
build_embedding_provider(), and build_vector_store() (app/providers.py,
app/vector_store.py) all treat AI_MODE=configured as the one switch that
turns on live network calls (Gemini/Ollama/Qdrant), independent of whether
API keys happen to be present in the environment. A developer's real .env
may legitimately carry live QDRANT_URL/QDRANT_API_KEY/GEMINI_API_KEY for
running the app — the test suite must never pick those up and start making
real network calls just because they're present. Tests that specifically
want to exercise the "configured" path already do so explicitly via
monkeypatch.setattr(settings, "ai_mode", "configured") plus a fake/injected
provider, never by relying on real credentials from the environment.
"""
import os
import tempfile

_data_dir = tempfile.mkdtemp(prefix="copilot-test-data-")
os.environ.setdefault("DATA_DIR", _data_dir)
os.environ["AI_MODE"] = "mock"
