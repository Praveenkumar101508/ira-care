"""The operations runbooks name only scripts, files and compose services that exist.

A runbook is read during an incident, which is the worst moment to find a renamed script.
So every repository path the runbooks cite, and every compose service in their commands,
is checked here."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
RUNBOOKS = ROOT / "docs/ops/RUNBOOKS.md"
SERVICES = set(yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text())["services"])
# Paths inside the backup image map to the repository's deploy/ scripts (deploy/Dockerfile.backup).
IMAGE_PATHS = {"/usr/local/bin/backup.sh": "deploy/backup.sh", "/usr/local/bin/restore.sh": "deploy/restore.sh",
               "/usr/local/bin/offsite.sh": "deploy/offsite.sh"}


def cited_paths(text: str) -> set[str]:
    found = set(re.findall(r"`((?:docs|deploy|scripts|supracloud-jarvis|core|care)/[\w./-]+?)`", text))
    for image_path in re.findall(r"/usr/local/bin/[\w.]+", text):
        found.add(IMAGE_PATHS.get(image_path, image_path))
    return found


def compose_services(text: str) -> set[str]:
    return set(re.findall(r"docker compose (?:exec|restart|ps|logs(?: --\S+)*(?: \d+\w*)*) (\w[\w-]*)", text))


#: The operator's own settings file: made at deployment (docs/DEPLOY.md section 3), never committed.
OPERATOR_FILES = {"deploy/.env"}


def _resolve(p: str) -> bool:
    return p in OPERATOR_FILES or (ROOT / p).exists() or (ROOT / "supracloud-jarvis/ira" / p).exists()


def test_every_cited_path_exists():
    missing = sorted(p for p in cited_paths(RUNBOOKS.read_text()) if not _resolve(p))
    assert missing == []


def test_every_compose_service_named_exists():
    named = compose_services(RUNBOOKS.read_text())
    assert named, "the runbooks name services"
    assert sorted(named - SERVICES) == []


def test_a_renamed_script_or_service_is_caught():
    """Negative controls: the checks object to a path or a service that is not there."""
    assert not _resolve("deploy/no_such_script.sh")
    assert compose_services("docker compose restart wrokr") - SERVICES == {"wrokr"}
