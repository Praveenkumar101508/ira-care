"""Every distributed artefact carries its third-party notices (K.d, owner directive section 7).

The notices files (docs/legal/notices/<artefact>.txt) were rendered but stayed in the
repository. Now each artefact's own build puts its file inside it:

* the three container images: their Dockerfile copies the file to
  /usr/share/doc/ira-care/THIRD_PARTY_NOTICES.txt, in the stage the image is built from, and CI
  compares the file inside each built image with the committed one;
* the dashboard: its copy (dashboard/src/legal/) is traced into the standalone output and shown
  at /licences;
* the family app: its copy (mobile/assets/legal/, JSON) is bundled and shown under Settings;
* the watch: each flavour's source set carries that build's file (src/<flavour>/assets/), which
  the release APK check (scripts/supply_chain.py apk) records, and About shows it.

No Docker, Gradle or network needed. Each check has a negative control.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts"))

import supply_chain as sc  # noqa: E402


def _final_stage(dockerfile: str) -> str:
    """The part of a Dockerfile after its last FROM: what the built image is."""
    return re.split(r"(?m)^FROM\s", dockerfile)[-1]


def dockerfile_problems(artefact: str, dockerfile: str) -> list[str]:
    want = f"COPY docs/legal/notices/{artefact}-image.txt {sc.IMAGE_NOTICES_PATH}"
    if want not in _final_stage(dockerfile):
        return [f"{artefact}: the image does not carry its notices ({want!r} not in its final stage)"]
    return []


def app_problems() -> list[str]:
    out = []
    for artefact, path in sc.APP_COPIES.items():
        notices = (sc.NOTICES_DIR / f"{artefact}.txt").read_text(encoding="utf-8")
        if not path.is_file():
            out.append(f"{artefact}: {sc._shown(path)} is missing")
        elif path.read_text(encoding="utf-8") != sc.app_copy_text(artefact, notices):
            out.append(f"{artefact}: {sc._shown(path)} is not its notices file")
    return out


def coverage_problems(images: dict, app_copies: dict) -> list[str]:
    """Every artefact that is distributed has a way to carry its notices."""
    carried = {f"{a}-image" for a in images} | set(app_copies)
    return [f"{a}: nothing carries its notices" for a in sorted(set(sc.ARTEFACTS) - carried)]


@pytest.mark.parametrize("artefact", sorted(sc.IMAGES))
def test_each_image_copies_its_notices_into_the_image(artefact):
    dockerfile = (ROOT / sc.IMAGES[artefact]["dockerfile"]).read_text(encoding="utf-8")
    assert dockerfile_problems(artefact, dockerfile) == []
    # Negative controls: without the line, or with it only in the build stage, it is caught.
    line = f"COPY docs/legal/notices/{artefact}-image.txt {sc.IMAGE_NOTICES_PATH}\n"
    assert dockerfile_problems(artefact, dockerfile.replace(line, ""))
    assert dockerfile_problems(artefact, f"FROM alpine AS build\n{line}FROM scratch\nCOPY --from=build / /\n")


def test_every_distributed_artefact_has_a_way_to_carry_its_notices():
    assert coverage_problems(sc.IMAGES, sc.APP_COPIES) == []
    without_phone = {k: v for k, v in sc.APP_COPIES.items() if k != "phone-app"}
    assert coverage_problems(sc.IMAGES, without_phone) == ["phone-app: nothing carries its notices"]


def test_each_app_bundles_its_own_notices_file_unedited():
    assert app_problems() == []
    assert sc.render(write=False) == []  # the copies are rendered like the notices themselves
    phone = json.loads(sc.APP_COPIES["phone-app"].read_text(encoding="utf-8"))
    assert phone["artefact"] == "phone-app" and phone["text"].startswith("THIRD-PARTY NOTICES: ")


def test_negative_control_a_stale_app_copy_is_caught(tmp_path, monkeypatch):
    copies = {}
    for artefact, path in sc.APP_COPIES.items():
        copies[artefact] = tmp_path / artefact / path.name
        copies[artefact].parent.mkdir()
        copies[artefact].write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(sc, "APP_COPIES", copies)
    assert app_problems() == []
    watch = copies["watch-aosp"]
    watch.write_text(watch.read_text(encoding="utf-8").replace("tensorflow-lite", "x"), encoding="utf-8")
    assert any("watch-aosp" in p for p in app_problems())
    assert any("watch-aosp" in p for p in sc.render(write=False))
    copies["phone-app"].unlink()
    assert any("phone-app" in p and "missing" in p for p in app_problems())


def test_each_watch_flavour_carries_its_own_file_and_the_apk_check_knows_it():
    for flavour in ("wearos", "aosp"):
        path = sc.APP_COPIES[f"watch-{flavour}"]
        assert path.parent == ROOT / "watch" / "app" / "src" / flavour / "assets"
    assert not (ROOT / "watch" / "app" / "src" / "main" / "assets" / "THIRD_PARTY_NOTICES.txt").exists()
    assert "assets/THIRD_PARTY_NOTICES.txt" in sc.WATCH_ASSETS
    contents = sc.load_json(sc.APK_CONTENTS)
    for flavour, c in contents.items():
        got = {a["file"]: a["sha256"] for a in c["assets"]}
        copy = sc.APP_COPIES[f"watch-{flavour}"].read_bytes()
        assert got.get("assets/THIRD_PARTY_NOTICES.txt") == sc.sha256(copy), flavour
    # Negative control: the other flavour's file is not this one's.
    assert sc.sha256(sc.APP_COPIES["watch-aosp"].read_bytes()) != \
        sc.sha256(sc.APP_COPIES["watch-wearos"].read_bytes())


def test_the_dashboard_build_traces_its_copy_and_the_image_keeps_it():
    config = (ROOT / "dashboard" / "next.config.ts").read_text(encoding="utf-8")
    rel = sc.APP_COPIES["dashboard-image"].relative_to(ROOT / "dashboard")
    assert f"'/licences': ['./{rel.as_posix()}']" in config
    dockerfile = (ROOT / "deploy" / "Dockerfile.dashboard").read_text(encoding="utf-8")
    assert "COPY --from=build --chown=ira:ira /app/.next/standalone ./" in _final_stage(dockerfile)


def test_the_phone_app_bundles_the_json_and_shows_it_from_settings():
    screen = (ROOT / "mobile" / "src" / "screens" / "LicencesScreen.tsx").read_text(encoding="utf-8")
    assert "require('../../assets/legal/THIRD_PARTY_NOTICES.json')" in screen
    settings = (ROOT / "mobile" / "src" / "shell" / "SettingsTab.tsx").read_text(encoding="utf-8")
    assert "key: 'licences'" in settings and "<LicencesView" in settings


def _steps(job: str) -> list[str]:
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    return [s.get("run", "") for s in jobs["jobs"][job]["steps"]]


def ci_problems(runs: list[str]) -> list[str]:
    step = next((r for r in runs if sc.IMAGE_NOTICES_PATH in r), "")
    return [f"CI does not compare the {a} image's notices with the committed file"
            for a in sorted(sc.IMAGES) if f"docs/legal/notices/{a}-image.txt" not in step
            and "docs/legal/notices/$i-image.txt" not in step]


def test_ci_compares_the_notices_inside_each_built_image():
    runs = _steps("images")
    assert ci_problems(runs) == []
    build = next(i for i, r in enumerate(runs) if "Dockerfile.backup" in r)
    check = next(i for i, r in enumerate(runs) if sc.IMAGE_NOTICES_PATH in r)
    assert build < check
    # Negative control: the images job without that step is caught.
    assert ci_problems([r for r in runs if sc.IMAGE_NOTICES_PATH not in r])
