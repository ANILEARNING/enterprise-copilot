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
"""
import os
import tempfile

_data_dir = tempfile.mkdtemp(prefix="copilot-test-data-")
os.environ.setdefault("DATA_DIR", _data_dir)
