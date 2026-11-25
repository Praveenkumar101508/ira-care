"""What the distributed artefacts contain is recorded, checked against the licence policy, and
turned into SBOMs and notices (K.d, owner directive section 7).

`scripts/supply_chain.py` scans built images and the watch's release APKs, and renders the
CycloneDX SBOMs (`docs/security/sbom/`) and the third-party notices (`docs/legal/`) from the
committed inventories; `scripts/licence_check.py` now also reads the watch's Gradle classpath.
These tests need no Docker, Gradle or network: they check the parsers on synthetic input, the
committed inventories against the policy, that the rendered files match the inventories, and
the CI wiring. Every check has a negative control that breaks a copy and expects a failure."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts"))

import licence_check as lc  # noqa: E402
import supply_chain as sc  # noqa: E402


# --- the policy additions --------------------------------------------------------------------

@pytest.mark.parametrize("declared", [
    "GPL-3.0-or-later WITH GCC-exception-3.1",  # libgcc, libstdc++, libgfortran, libgomp
    "Apache-2.0 WITH LLVM-exception",
    "FTL", "IJG AND BSD-3-Clause", "libpng-2.0", "PostgreSQL", "OLDAP-2.8", "ICU",
])
def test_runtime_exceptions_and_permissive_native_licences_are_allowed(declared):
    assert lc.classify(declared).allowed, lc.classify(declared).reason


@pytest.mark.parametrize("declared", [
    "GPL-2.0-only WITH GCC-exception-3.1",       # the exception is for GCC's GPL-3.0 runtime
    "GPL-2.0-only WITH Classpath-exception-2.0",  # still reviewed one by one
    "GPL-3.0-or-later",                           # no exception, no permission to link
    "Sleepycat",
])
def test_negative_control_other_with_clauses_and_copyleft_stay_forbidden(declared):
    assert not lc.classify(declared).allowed


# --- Gradle: POM licences --------------------------------------------------------------------

def _pom(home: Path, g: str, a: str, v: str, body: str) -> None:
    d = home / "caches" / "modules-2" / "files-2.1" / g / a / v / "0123abcd"
    d.mkdir(parents=True)
    (d / f"{a}-{v}.pom").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
        f"<groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version>{body}"
        "</project>", encoding="utf-8")


def _poms(home: Path) -> dict[str, str]:
    """{coordinate: path} for the POMs written by _pom, as the init script reports them."""
    return {":".join(p.relative_to(home / "caches/modules-2/files-2.1").parts[:3]): str(p)
            for p in home.rglob("*.pom")}


def test_pom_licence_names_and_urls_map_to_spdx(tmp_path):
    _pom(tmp_path, "g", "a", "1", "<licenses><license><name>The Apache Software License, "
         "Version 2.0</name></license></licenses>")
    _pom(tmp_path, "g", "b", "1", "<licenses><license><name>whatever</name>"
         "<url>https://opensource.org/licenses/MIT</url></license></licenses>")
    assert lc.pom_declared("g", "a", "1", _poms(tmp_path)) == ("Apache-2.0", "POM", "jar")
    assert lc.pom_declared("g", "b", "1", _poms(tmp_path))[0] == "MIT"


def test_the_result_does_not_depend_on_the_local_gradle_cache(tmp_path, monkeypatch):
    _pom(tmp_path / "a", "x", "lib", "1", "<packaging>pom</packaging>")
    poms = _poms(tmp_path / "a")
    monkeypatch.setattr(lc, "gradle_modules", lambda eco: ({"wearos": ["x:lib:1"],
                                                            "aosp": []}, poms))
    sections = []
    for home in (tmp_path / "empty-home", tmp_path / "a"):
        monkeypatch.setenv("GRADLE_USER_HOME", str(home))
        sections.append(lc.gradle_section("gradle-watch", []))
    assert sections[0] == sections[1] and sections[0]["no_code_modules"] == ["x:lib@1"]
    # Negative control: a module whose POM Gradle did not report is never silently allowed.
    monkeypatch.setattr(lc, "gradle_modules", lambda eco: ({"wearos": ["x:lib:1"],
                                                            "aosp": []}, {}))
    sec = lc.gradle_section("gradle-watch", [])
    assert sec["packages"]["x:lib@1"]["status"] == "forbidden"


def test_pom_without_licences_follows_its_parent(tmp_path):
    _pom(tmp_path, "g", "parent", "2", "<packaging>pom</packaging><licenses><license>"
         "<name>BSD-3-Clause</name></license></licenses>")
    _pom(tmp_path, "g", "child", "1", "<parent><groupId>g</groupId><artifactId>parent"
         "</artifactId><version>2</version></parent><packaging>aar</packaging>")
    assert lc.pom_declared("g", "child", "1", _poms(tmp_path)) == ("BSD-3-Clause",
                                                           "parent POM g:parent:2", "aar")


def test_negative_control_a_pom_with_no_licence_or_a_gpl_one_is_forbidden(tmp_path):
    _pom(tmp_path, "g", "none", "1", "")
    _pom(tmp_path, "g", "gpl", "1", "<licenses><license><name>GNU General Public License v3 "
         "(GPLv3)</name></license></licenses>")
    declared, source, _ = lc.pom_declared("g", "none", "1", _poms(tmp_path))
    assert source == "POM names no licence" and not lc.classify(declared).allowed
    assert not lc.classify(lc.pom_declared("g", "gpl", "1", _poms(tmp_path))[0]).allowed
    assert lc.pom_declared("g", "missing", "1", _poms(tmp_path))[1] == "no POM resolved"


def test_gradle_section_reads_both_flavours_and_sets_aside_no_code_modules(tmp_path,
                                                                           monkeypatch):
    """The POMs come from what Gradle reports, not from wherever a cache happens to be: the
    section is the same on any machine (the CI failure on PR #105 was a cache lookup)."""
    _pom(tmp_path, "x", "lib", "1", "<licenses><license><name>MIT</name></license></licenses>")
    _pom(tmp_path, "x", "bom", "1", "<packaging>pom</packaging>")
    _pom(tmp_path, "x", "gms", "1", "<licenses><license><name>Proprietary</name></license>"
         "</licenses>")
    monkeypatch.setattr(lc, "gradle_modules", lambda eco: (
        {"wearos": ["x:lib:1", "x:bom:1", "x:gms:1"], "aosp": ["x:lib:1"]}, _poms(tmp_path)))
    sec = lc.gradle_section("gradle-watch", [])
    assert sec["flavours"] == {"aosp": ["x:lib@1"], "wearos": ["x:gms@1", "x:lib@1"]}
    assert sec["no_code_modules"] == ["x:bom@1"]
    # Negative control: the proprietary module fails the policy.
    inv = {"ecosystems": {"gradle-watch": sec}}
    assert any("x:gms" in p and "FORBIDDEN" in p
               for p in lc.policy_problems(inv, [], ("gradle-watch",)))


# --- the committed watch inventory and APK contents ------------------------------------------

def _inv() -> dict:
    return lc.load_inventory()


def test_the_watch_classpath_is_inventoried_for_both_flavours_and_obeys_the_policy():
    sec = _inv()["ecosystems"]["gradle-watch"]
    assert lc.policy_problems(_inv(), lc.load_exceptions(), ("gradle-watch",)) == []
    for flavour in ("wearos", "aosp"):
        keys = sec["flavours"][flavour]
        assert keys and set(keys) <= set(sec["packages"])
        assert any(k.startswith("org.tensorflow:tensorflow-lite@") for k in keys)
    # The plain Android build carries no Google service library (the CI rule, here too).
    assert not [k for k in sec["flavours"]["aosp"]
                if k.startswith(("com.google.android.gms", "androidx.health:"))]
    assert any(k.startswith("androidx.health:health-services-client@")
               for k in sec["flavours"]["wearos"])


def test_the_release_apks_carry_the_v3_fall_model_and_no_native_library_of_unknown_origin():
    apk = sc.load_json(sc.APK_CONTENTS)
    assert set(apk) == {"wearos", "aosp"}
    for flavour, c in apk.items():
        files = {a["file"]: a for a in c["assets"]}
        assert files["assets/fall_model.tflite"]["sha256"] == sc.RELEASE_FALL_MODEL_SHA256
        assert {n["file"] for n in c["native_libraries"]} >= {"libtensorflowlite_jni.so"}
        assert all(n["from"] for n in c["native_libraries"]), flavour
        assert c["library_version_files_checked"] > 20


@pytest.mark.parametrize("stem, text, ok", [
    ("androidx.core_core", "1.15.0", True),
    ("androidx.core_core", "1.0.0", False),                    # another version than resolved
    ("androidx.arch.core_core-runtime", "task ':x' property 'version'", True),  # upstream bug
    ("androidx.unknown_lib", "1.0.0", False),                  # not on the classpath at all
    ("kotlinx_coroutines_core", "1.9.0", True),
    ("kotlinx_coroutines_core", "0.1.0", False),
])
def test_library_version_files_must_match_the_resolved_classpath(stem, text, ok):
    known = {"androidx.core:core@1.15.0", "androidx.arch.core:core-runtime@2.2.0",
             "org.jetbrains.kotlinx:kotlinx-coroutines-core-jvm@1.9.0"}
    assert sc.version_file_matches(stem, text, known) is ok


# --- images: synthetic scans -----------------------------------------------------------------

def _facts(needed: list[str], bundled: bool = False) -> dict:
    site = "/usr/local/lib/python3.12/site-packages"
    ext = f"{site}/pkg/_ext.cpython-312-x86_64-linux-gnu.so"
    record = "pkg/_ext.cpython-312-x86_64-linux-gnu.so,,\n"
    elf = {ext: {"sha256": "0" * 64, "size": 1, "soname": "", "needed": needed},
           "/lib/x86_64-linux-gnu/libreadline.so.8.2": {"sha256": "1" * 64, "size": 1,
                                                       "soname": "libreadline.so.8",
                                                       "needed": []}}
    if bundled:
        lib = f"{site}/pkg.libs/libmystery-0a1b2c3d.so.1"
        elf[lib] = {"sha256": "2" * 64, "size": 1, "soname": "", "needed": []}
        record += "pkg.libs/libmystery-0a1b2c3d.so.1,,\n"
    return {
        "elf": elf, "links": {"/lib/x86_64-linux-gnu/libreadline.so.8": "libreadline.so.8.2"},
        "dpkg_status": "Package: libreadline8t64\nStatus: install ok installed\nVersion: 8.2-6\n",
        "dpkg_lists": {"libreadline8t64": ["/lib/x86_64-linux-gnu/libreadline.so.8.2",
                                           "/lib/x86_64-linux-gnu/libreadline.so.8"]},
        "deb_copyright": {"libreadline8t64": "Files: *\nCopyright: 1989-2020 FSF\n"
                                             "License: GPL-3+\n"},
        "apk_db": "", "npm": {}, "os_release": 'ID=debian\nVERSION_ID="13"\n', "models": {},
        "dists": {(site, "pkg-1.0"): {"metadata": "Name: pkg\nVersion: 1.0\nLicense-Expression: "
                                                  "MIT\n", "record": record,
                                      "licence_files": {"LICENSE": "Copyright (c) 2026 Pkg "
                                                                   "Authors\n"}}},
    }


def _scan(facts: dict, review_os: dict, review: dict | None = None) -> dict:
    return sc.image_inventory("api", facts, None, [], review or {}, review_os)


def test_an_image_scan_records_who_links_each_os_library_and_reads_package_metadata():
    inv = _scan(_facts(["libreadline.so.8"]),
                {"libreadline8t64": {"licence": "BSD-3-Clause", "evidence": "test"}})
    assert inv["python"]["pkg@1.0"]["status"] == "allowed"
    assert inv["python"]["pkg@1.0"]["copyright"] == ["Copyright (c) 2026 Pkg Authors"]
    assert inv["os_packages"]["libreadline8t64@8.2-6"]["linked_by"] == ["pkg@1.0"]
    assert sc.image_problems(inv) == []


def test_negative_control_a_linked_gpl_os_library_fails_the_image_check():
    gpl = {"libreadline8t64": {"licence": "GPL-3.0-or-later", "evidence": "test"}}
    problems = sc.image_problems(_scan(_facts(["libreadline.so.8"]), gpl))
    assert any("libreadline8t64" in p and "FORBIDDEN" in p for p in problems), problems
    # Not linked by any of our components: recorded, not judged (aggregation).
    unlinked = _scan(_facts([]), gpl)
    assert unlinked["linked_os_libraries"] == {} and sc.image_problems(unlinked) == []
    assert unlinked["os_packages"]["libreadline8t64@8.2-6"]["gpl_named_in_copyright_file"]
    # A linked OS library nobody has reviewed fails too.
    assert any("not in" in p for p in sc.image_problems(_scan(_facts(["libreadline.so.8"]), {})))


def test_negative_control_an_unreviewed_native_library_inside_a_wheel_fails():
    inv = _scan(_facts([], bundled=True), {})
    e = next(iter(inv["bundled_native_libraries"].values()))
    assert e["name"] == "libmystery" and e["shipped_in"] == "pkg@1.0"
    assert any("libmystery" in p for p in sc.image_problems(inv))
    ok = _scan(_facts([], bundled=True), {},
               {"libmystery": {"licence": "MIT", "evidence": "test", "copyright": []}})
    assert sc.image_problems(ok) == []


def test_negative_control_a_forbidden_python_package_in_an_image_fails():
    facts = _facts([])
    dist = facts["dists"][next(iter(facts["dists"]))]
    dist["metadata"] = "Name: pkg\nVersion: 1.0\nLicense-Expression: AGPL-3.0-only\n"
    assert any("pkg@1.0" in p and "FORBIDDEN" in p for p in sc.image_problems(_scan(facts, {})))


def test_the_elf_reader_finds_needed_libraries_and_ignores_other_files():
    exe = Path(sys.executable).resolve()
    data = exe.read_bytes()
    if data[:4] != b"\x7fELF":
        pytest.skip("the interpreter is not an ELF file here")
    got = sc.elf_dynamic(data)
    assert got is not None and any(n.startswith("libc.so") for n in got[1]), got
    assert sc.elf_dynamic(b"#!/bin/sh\necho not elf\n") is None


@pytest.mark.parametrize("name, stem", [
    ("libgfortran-040039e1.so.5.0.0", "libgfortran"),
    ("libopenblas64_p-r0-0cf96a72.3.23.dev.so", "libopenblas64_p"),
    ("libtensorflowlite_jni.so", "libtensorflowlite_jni"),
])
def test_native_library_names_lose_auditwheels_hash(name, stem):
    assert sc.native_stem(name) == stem


# --- images: the committed snapshots ---------------------------------------------------------

def _image(name: str) -> dict:
    return sc.load_json(sc.image_inventory_path(name))


@pytest.mark.parametrize("name", sorted(sc.IMAGES))
def test_every_image_snapshot_obeys_the_policy(name):
    inv = _image(name)
    assert inv["artefact"] == f"{name}-image" and inv["note"]
    assert sc.image_problems(inv, lc.load_exceptions()) == []
    review_bundled, review_os = sc.load_native_review()
    assert {e["name"] for e in inv["bundled_native_libraries"].values()} <= set(review_bundled)
    assert {k.split("@")[0] for k in inv["linked_os_libraries"]} <= set(review_os)


def test_negative_control_dropping_an_image_exception_or_editing_a_verdict_is_caught():
    inv = _image("api")
    without = [x for x in lc.load_exceptions() if x["package"] != "libc6"]
    assert any("libc6" in p for p in sc.image_problems(inv, without))
    edited = copy.deepcopy(_image("dashboard"))
    e = next(e for e in edited["npm"].values() if e["name"] == "@img/sharp-wasm32")
    e["status"] = "allowed"
    assert any("sharp-wasm32" in p and "recorded allowed" in p
               for p in sc.image_problems(edited, lc.load_exceptions()))


def test_the_api_image_links_no_gpl_library_and_its_dockerfile_keeps_it_that_way():
    inv = _image("api")
    linked = {k.split("@")[0] for k in inv["linked_os_libraries"]}
    assert not linked & {"libreadline8t64", "libgdbm6t64", "libdb5.3t64"}, linked
    assert "libreadline8t64" in {k.split("@")[0] for k in inv["os_packages"]}  # present, unlinked
    dockerfile = (ROOT / "deploy" / "Dockerfile.api").read_text(encoding="utf-8")
    for module in ("readline", "_gdbm", "_dbm"):
        assert f"lib-dynload/{module}.cpython-312-*.so" in dockerfile
    # Negative control: the snapshot taken before the change linked all three.
    before = copy.deepcopy(inv)
    before["linked_os_libraries"]["libreadline8t64@8.2-6"] = lc.entry(
        "os-linked", "libreadline8t64", "8.2-6", "GPL-3.0-or-later", "upstream", [])
    assert any("libreadline8t64" in p for p in sc.image_problems(before))


def test_the_api_image_snapshot_carries_the_models_the_notices_attribute():
    files = _image("api")["model_files"]
    for m in sc.MODELS["api"]:
        assert m["file"] in files, m["file"]
    assert not any(p.endswith(".tflite") for p in files)  # the watch's models stay on the watch


def test_exceptions_for_what_only_the_images_contain_match_the_snapshots():
    assert sc.committed_exception_problems(lc.load_exceptions()) == []
    stale = lc.load_exceptions() + [{
        "ecosystem": "image-api", "package": "left-years-ago", "licence": "GPL-3.0-only",
        "reason": "Was in the image once and has since been removed from it entirely."}]
    assert any("left-years-ago" in p for p in sc.committed_exception_problems(stale))
    assert lc.exception_problems(lc.load_exceptions()) == []


# --- SBOMs and notices match the inventories -------------------------------------------------

def test_sboms_and_notices_are_exactly_what_the_inventories_render():
    assert sc.render(write=False) == []


def test_negative_control_a_hand_edited_notices_file_is_caught(tmp_path, monkeypatch):
    for d in ("notices",):
        (tmp_path / d).mkdir()
    for f in sc.NOTICES_DIR.iterdir():
        (tmp_path / "notices" / f.name).write_text(f.read_text(encoding="utf-8"),
                                                   encoding="utf-8")
    monkeypatch.setattr(sc, "NOTICES_DIR", tmp_path / "notices")
    assert sc.render(write=False) == []
    target = tmp_path / "notices" / "watch-wearos.txt"
    target.write_text(target.read_text(encoding="utf-8").replace("tensorflow-lite", "x"),
                      encoding="utf-8")
    assert any("watch-wearos.txt" in p for p in sc.render(write=False))


def _missing_from_notices(text: str, comps: list[dict]) -> list[str]:
    return [c["ref"] for c in comps if c["kind"] != "model"
            and f"{c['name']} {c['version']}".rstrip() + "\n" not in text]


@pytest.mark.parametrize("artefact", sorted(sc.ARTEFACTS))
def test_the_notices_list_every_distributed_package(artefact):
    text = (sc.NOTICES_DIR / f"{artefact}.txt").read_text(encoding="utf-8")
    comps = sc.artefact_components(artefact)
    assert comps and _missing_from_notices(text, comps) == []
    for m in [c for c in comps if c["kind"] == "model"]:
        assert m["sha256"] in text
        for d in m.get("datasets", []):
            assert sc.dataset_rows()[d]["citation"].replace("*", "") in text
    # Negative control: a notices file without one package is caught.
    first = next(c for c in comps if c["kind"] != "model")
    line = f"{first['name']} {first['version']}".rstrip() + "\n"
    assert _missing_from_notices(text.replace(line, ""), comps) == [first["ref"]]


def test_the_notices_cover_every_shipped_package_of_the_source_inventories():
    inv = _inv()["ecosystems"]
    watch = {c["ref"] for c in sc.artefact_components("watch-wearos")}
    assert {sc._purl("maven", inv["gradle-watch"]["packages"][k]["name"],
                     inv["gradle-watch"]["packages"][k]["version"])
            for k in inv["gradle-watch"]["flavours"]["wearos"]} <= watch
    phone = {c["ref"] for c in sc.artefact_components("phone-app")}
    assert len(phone) == len(inv["npm-mobile"]["packages"])
    dash = {c["ref"] for c in sc.artefact_components("dashboard-image")}
    assert {sc._purl("npm", e["name"], e["version"])
            for e in inv["npm-dashboard"]["packages"].values()} <= dash


@pytest.mark.parametrize("artefact", sorted(sc.ARTEFACTS))
def test_every_sbom_is_cyclonedx_and_lists_exactly_the_artefacts_components(artefact):
    bom = sc.load_json(sc.SBOM_DIR / f"{artefact}.cdx.json")
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.5"
    assert "serialNumber" not in bom and "timestamp" not in bom["metadata"]  # deterministic
    refs = [c["bom-ref"] for c in bom["components"]]
    assert len(refs) == len(set(refs))
    assert refs == [c["ref"] for c in sc.artefact_components(artefact)]
    for c in bom["components"]:
        for lic in c["licenses"]:
            if "expression" in lic:
                assert lc.normalise(lic["expression"]) is not None, c["bom-ref"]
        status = next(p["value"] for p in c["properties"] if p["name"] == "ira:licence-status")
        assert status != "forbidden", c["bom-ref"]
    # Negative control: an SBOM missing a component no longer matches.
    assert refs[1:] != [c["ref"] for c in sc.artefact_components(artefact)]


def test_every_model_dataset_is_a_row_of_the_dataset_register(monkeypatch):
    rows = sc.dataset_rows()
    for models in sc.MODELS.values():
        for m in models:
            for d in m["datasets"]:
                assert "CC BY 4.0" in rows[d]["licence"], d
    broken = copy.deepcopy(sc.MODELS)
    broken["watch"][0]["datasets"] = ["A Dataset Nobody Recorded"]
    monkeypatch.setattr(sc, "MODELS", broken)
    assert any("A Dataset Nobody Recorded" in p for p in sc.render(write=False))


def test_every_licence_named_in_the_notices_has_its_text():
    need = {t for a in sc.ARTEFACTS for t in sc.needed_licence_texts(sc.artefact_components(a))}
    assert {"Apache-2.0", "MIT", "LGPL-3.0-or-later"} <= need
    assert [t for t in need if not (sc.LICENCE_TEXTS / f"{t}.txt").is_file()] == []


# --- CI ---------------------------------------------------------------------------------------

def _steps(job: str) -> list[str]:
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    return [s.get("run", "") for s in jobs["jobs"][job]["steps"]]


def _order(runs: list[str], *needles: str) -> list[int | None]:
    return [next((i for i, r in enumerate(runs) if n in r), None) for n in needles]


def test_ci_checks_the_watch_release_build_and_the_images():
    w = _order(_steps("watch"), "assembleWearosRelease", "--only gradle-watch",
               "supply_chain.py apk --check")
    assert None not in w and w == sorted(w), w
    runs = _steps("images")
    i = _order(runs, "Dockerfile.backup", "supply_chain.py image api",
               "supply_chain.py image dashboard", "supply_chain.py image backup", "syft")
    assert None not in i and i[0] < i[1], i
    assert "sha256sum -c" in runs[i[4]]
    # Negative control: the watch job without the APK check is caught.
    stripped = [r for r in _steps("watch") if "supply_chain.py apk" not in r]
    assert None in _order(stripped, "supply_chain.py apk --check")
