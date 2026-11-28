"""The care API answers a structured refusal with its status, never a 500 (round 10).

scripts/demo/api.py is the app the IRA CARE journeys run on (the dashboard, the phone app and the
browser tests). It did not install the shared error handlers, so a refusal raised as `IRAError`
("step-up required" among them) escaped as 500 Internal Server Error; the browser read that as
"No connection to the server" and never asked the administrator to confirm who they are. Found by
e2e/deployment_admin.spec.ts on 27 November 2026.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

pytest.importorskip("fastapi")

#: The edition before this module imports the demo API, which sets IRA_EDITION=cloud by default.
EDITION_BEFORE = os.environ.get("IRA_EDITION")


@pytest.fixture(scope="module")
def demo_app():
    # Importing the demo API sets IRA_EDITION=cloud and other defaults in os.environ and fills the
    # settings cache; both are put back afterwards, or every later test in the process would run
    # as the cloud edition (CI run 46032445487: 73 unrelated failures until this was restored).
    from config import get_settings
    saved_env = dict(os.environ)
    get_settings.cache_clear()
    spec = importlib.util.spec_from_file_location("care_demo_api_under_test", ROOT / "scripts" / "demo" / "api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = mod.create_demo_app()
    from core.runtime.errors import STEP_UP_REQUIRED, IRAError

    @app.get("/__refuse_for_test")
    async def refuse():
        raise IRAError(STEP_UP_REQUIRED, safe_details={"reason": "authentication_too_old"})

    yield app
    os.environ.clear()
    os.environ.update(saved_env)
    get_settings.cache_clear()
    sys.modules.pop(spec.name, None)


def test_the_environment_is_put_back_afterwards():
    """Runs after the module's tests in file order: the import's cloud default is gone again."""
    assert os.environ.get("IRA_EDITION") == EDITION_BEFORE


def test_the_care_api_installs_the_shared_error_handlers(demo_app):
    from core.runtime.errors import IRAError
    assert IRAError in demo_app.exception_handlers


def test_a_step_up_refusal_is_a_403_with_its_code_not_a_500(demo_app):
    from fastapi.testclient import TestClient
    # No lifespan: the route under test touches no database.
    client = TestClient(demo_app, raise_server_exceptions=False)
    r = client.get("/__refuse_for_test")
    assert r.status_code == 403, r.text
    assert r.json()["error_code"] == "step_up_required"
