"""The assembled application exposes the care API under /api/v1/care."""
from __future__ import annotations

import os
import sys

import pytest

for _k in ("IRA_SECRET_KEY", "IRA_ADMIN_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "VLLM_API_KEY"):
    os.environ.setdefault(_k, "test-placeholder")

pytest.importorskip("slowapi")

_st = sys.modules.get("sentence_transformers")
if _st is not None and not hasattr(_st, "SentenceTransformer"):
    _st.SentenceTransformer = type("SentenceTransformer", (), {})

from app.factory import create_app  # noqa: E402
from tests.route_introspection import route_paths  # noqa: E402

EXPECTED = {
    "/api/v1/care/people",
    "/api/v1/care/people/{person_id}",
    "/api/v1/care/people/{person_id}/status",
    "/api/v1/care/people/{person_id}/circle",
    "/api/v1/care/people/{person_id}/settings",
    "/api/v1/care/people/{person_id}/devices",
    "/api/v1/care/devices/pair",
    "/api/v1/care/devices/me",
    "/api/v1/care/devices/me/report",
    "/api/v1/care/devices/me/help",
    "/api/v1/care/people/{person_id}/vitals",
    "/api/v1/care/people/{person_id}/checkins",
    "/api/v1/care/alerts",
    "/api/v1/care/alerts/{alert_id}/acknowledge",
    "/api/v1/care/alerts/{alert_id}/resolve",
    "/api/v1/care/people/{person_id}/routines",
    "/api/v1/care/people/{person_id}/medications",
    "/api/v1/care/people/{person_id}/medication-events",
    "/api/v1/care/people/{person_id}/transcripts",
    "/api/v1/care/people/{person_id}/consents",
    "/api/v1/care/people/{person_id}/access-log",
}


def test_every_care_route_is_mounted_under_the_api_prefix():
    paths = set(route_paths(create_app()))
    missing = sorted(EXPECTED - paths)
    assert not missing, f"care routes missing from the app: {missing}"


def test_care_routes_are_not_mounted_anywhere_else():
    stray = [p for p in route_paths(create_app()) if "/care/" in p and not p.startswith("/api/v1/care/")]
    assert not stray, stray
