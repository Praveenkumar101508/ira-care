"""Vault edit + key-rotation round-trips (need the AES body cipher)."""
import pytest

pytest.importorskip("cryptography")

from armory import vault as v  # noqa: E402


def test_reseal_preserves_edits(tmp_path):
    v.create_from_template(tmp_path, "k")
    files = v.read_bundle(tmp_path, "k")
    files[v.WHITELIST_FILE] += "\nexample.com\n"
    v.reseal(tmp_path, "k", files)
    contents = v.open(tmp_path, "k")
    assert contents.whitelist.authorizes("example.com") is True


def test_reseal_rejects_invalid_bundle(tmp_path):
    v.create_from_template(tmp_path, "k")
    bad = v.read_bundle(tmp_path, "k")
    bad[v.MANIFEST_FILE] = '{"tools": [{"id": "x", "image": "windows", "cmd": "x", "risk": "low"}]}'
    with pytest.raises(v.VaultError):
        v.reseal(tmp_path, "k", bad)


def test_rotate_key_preserves_contents(tmp_path):
    v.create_from_template(tmp_path, "oldkey")
    before = v.read_bundle(tmp_path, "oldkey")
    v.rotate_key(tmp_path, "oldkey", "newkey")
    with pytest.raises(v.VaultError):
        v.read_bundle(tmp_path, "oldkey")          # old key no longer opens it
    assert v.read_bundle(tmp_path, "newkey") == before


def test_rotate_key_wrong_old_key_leaves_vault_intact(tmp_path):
    v.create_from_template(tmp_path, "k")
    with pytest.raises(v.VaultError):
        v.rotate_key(tmp_path, "wrong-old", "new")
    # original key still works — a failed rotation wrote nothing
    assert v.open(tmp_path, "k") is not None


def test_rotate_key_rejects_empty_new_key(tmp_path):
    v.create_from_template(tmp_path, "k")
    with pytest.raises(v.VaultError):
        v.rotate_key(tmp_path, "k", "   ")
