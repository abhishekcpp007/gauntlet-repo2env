import subprocess
from pathlib import Path

import docker
import pytest


def _docker_ok() -> bool:
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False


DOCKER_OK = _docker_ok()


def pytest_collection_modifyitems(config, items):
    if DOCKER_OK:
        return
    skip = pytest.mark.skip(reason="Docker daemon not reachable")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _commit(repo: Path, message: str, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


PYPROJECT = """\
[project]
name = "calc"
version = "0.1.0"

[project.optional-dependencies]
test = ["pytest"]
docs = ["sphinx"]

[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["calc"]
"""


def make_calc_repo(root: Path) -> Path:
    """A tiny repo whose history holds two real tasks and two commits that aren't tasks."""
    repo = root / "calc"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(
        repo,
        "Initial calc with add",
        {
            "pyproject.toml": PYPROJECT,
            "calc/__init__.py": "def add(a, b):\n    return a + b\n",
            "tests/test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        },
    )
    _commit(
        repo,
        "Add mul\n\nMultiplication was missing.",
        {
            "calc/__init__.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
            "tests/test_calc.py": (
                "from calc import add, mul\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
                "def test_mul():\n    assert mul(3, 4) == 12\n"
            ),
        },
    )
    _commit(repo, "Write a README", {"README.md": "# calc\n"})
    _commit(
        repo,
        "Add sub",  # source only, no tests: not a task
        {
            "calc/ops.py": "def sub(a, b):\n    return b - a\n",
        },
    )
    _commit(
        repo,
        "Fix sub argument order\n\nsub(5, 3) returned -2.",
        {
            "calc/ops.py": "def sub(a, b):\n    return a - b\n",
            "tests/test_ops.py": (
                "from calc.ops import sub\n\n\ndef test_sub():\n    assert sub(5, 3) == 2\n\n\n"
                "class TestSubMore:\n    def test_zero(self):\n        assert sub(0, 0) == 0\n"
            ),
        },
    )
    return repo


@pytest.fixture
def calc_repo(tmp_path) -> Path:
    return make_calc_repo(tmp_path)
