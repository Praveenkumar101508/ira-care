"""No training script publishes model weights or makes a dataset public (D-082).

The original fall training run (scripts/training/kaggle_fall_model.py) tried to keep its
SisFall-trained weights as a Kaggle model under "Apache 2.0"; Kaggle refused it
("models.create" denied, docs/evidence/training/fall.json). The owner's rule since: a training
script never publishes model weights, and anything it pushes to Kaggle stays private.

Reads every Python file in scripts/training/ and fails when one:
  - calls a model-publishing function at all (kagglehub ``model_upload``, the Kaggle API's
    ``model_create_new`` / ``model_instance_create`` / ``model_instance_version_create``,
    Hugging Face ``push_to_hub`` / ``upload_file`` / ``upload_folder`` / ``create_repo``);
  - calls a dataset or kernel push (``dataset_create_new``, ``dataset_create_version``,
    ``kernels_push``) with public visibility (``public=True``, ``is_private=False``,
    ``private=False``);
  - writes Kaggle metadata with ``"is_private": False`` / ``"isPrivate": False``;
  - runs the Kaggle command line with ``--public`` or ``-u`` (``kaggle datasets create -u``).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
TRAINING = ROOT / "scripts" / "training"

PUBLISHES_WEIGHTS = {
    "model_upload", "model_create_new", "model_instance_create", "model_instance_version_create",
    "push_to_hub", "upload_file", "upload_folder", "create_repo",
}
PUSHES = {"dataset_create_new", "dataset_create_version", "kernels_push", "dataset_upload"}
PRIVATE_KEYS = {"is_private", "isPrivate", "private"}


def _name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_const(node: ast.expr | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _public_keyword(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "public" and _is_const(kw.value, True):
            return True
        if kw.arg in PRIVATE_KEYS and _is_const(kw.value, False):
            return True
    return False


def _cli_public(node: ast.List | ast.Tuple) -> bool:
    words = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return "kaggle" in words and ("--public" in words or "-u" in words)


def problems(source: str, name: str = "<script>") -> list[str]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            called = _name(node.func)
            if called in PUBLISHES_WEIGHTS:
                out.append(f"{name}:{node.lineno}: {called}() publishes model weights")
            elif called in PUSHES and _public_keyword(node):
                out.append(f"{name}:{node.lineno}: {called}() with public visibility")
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in PRIVATE_KEYS and _is_const(value, False):
                    out.append(f"{name}:{node.lineno}: Kaggle metadata with {key.value!r}: False")
        elif isinstance(node, ast.List | ast.Tuple) and _cli_public(node):
            out.append(f"{name}:{node.lineno}: kaggle command line with --public / -u")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            words = node.value.split()
            if words[:1] == ["kaggle"] and ("--public" in words or "-u" in words):
                out.append(f"{name}:{node.lineno}: kaggle command line with --public / -u")
    return out


SCRIPTS = sorted(TRAINING.glob("*.py"))


def test_there_are_training_scripts_to_check():
    names = {p.name for p in SCRIPTS}
    assert {"kaggle_fall_model.py", "run_kaggle.py", "kaggle_conversation_qlora.py"} <= names


@pytest.mark.parametrize("path", SCRIPTS, ids=[p.name for p in SCRIPTS])
def test_no_training_script_publishes_weights_or_a_public_dataset(path: Path):
    assert problems(path.read_text(encoding="utf-8"), path.name) == []


def test_the_check_notices_the_removed_fall_model_upload():
    # The block the original fall script carried (removed under D-082).
    removed = (
        "import kagglehub\n"
        "handle = 'owner/ira-care-fall-model/tfLite/v1'\n"
        "kagglehub.model_upload(handle, '.', license_name='Apache 2.0', version_notes='SisFall + UCI HAR')\n"
    )
    assert any("model_upload() publishes model weights" in p for p in problems(removed))
    # Put it back into the real script and the check objects.
    fall = (TRAINING / "kaggle_fall_model.py").read_text(encoding="utf-8")
    assert problems(fall) == []
    assert problems(fall + "\n" + removed) != []


def test_the_check_notices_public_dataset_pushes():
    assert problems("api.dataset_create_new(folder='d', public=True)") != []
    assert problems("api.dataset_create_new(folder='d', public=False)") == []
    assert problems("api.kernels_push(folder='k', is_private=False)") != []
    assert problems("meta = {'id': 'a/b', 'is_private': False}") != []
    assert problems("meta = {'id': 'a/b', 'is_private': True}") == []
    assert problems("subprocess.run(['kaggle', 'datasets', 'create', '-p', 'd', '-u'])") != []
    assert problems("subprocess.run(['kaggle', 'datasets', 'create', '-p', 'd'])") == []
    assert problems("os.system('kaggle datasets create -p d --public')") != []
