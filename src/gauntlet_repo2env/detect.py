"""Work out what kind of project a repo is and how to install it."""

from __future__ import annotations

import configparser
import fnmatch
import shlex
import tomllib
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from .repo import Repo, files_at, read_at

TEST_EXTRA_NAMES = ("test", "tests", "testing", "dev")

MARKERS = {
    "package.json": "node",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "java",
    "build.gradle": "java",
    "Gemfile": "ruby",
}


class Stack(BaseModel):
    language: str
    installable: bool = False
    extras: list[str] = Field(default_factory=list)
    dependency_groups: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    requires_python: str | None = None
    # the PEP 517 build backend, installed into the env so the project can be reinstalled
    # offline later (a task sandbox at an older commit has to regenerate files like _version.py)
    build_requires: list[str] = Field(default_factory=list)


def _is_requirements(path: str) -> bool:
    p = PurePosixPath(path)
    if len(p.parts) == 1:
        return fnmatch.fnmatch(p.name, "*requirements*.txt")
    return len(p.parts) == 2 and p.parts[0] == "requirements" and p.suffix == ".txt"


def detect(repo: Repo, sha: str) -> Stack:
    files = files_at(repo, sha)
    root = {f for f in files if "/" not in f}

    has_py_project = bool(root & {"pyproject.toml", "setup.py", "setup.cfg"})
    requirements = sorted(f for f in files if _is_requirements(f))
    if not has_py_project and not requirements:
        language = next((lang for marker, lang in MARKERS.items() if marker in root), "unknown")
        return Stack(language=language)

    stack = Stack(language="python", requirements=requirements)
    extras: set[str] = set()

    pyproject = read_at(repo, sha, "pyproject.toml") if "pyproject.toml" in root else None
    if pyproject:
        try:
            data = tomllib.loads(pyproject)
        except tomllib.TOMLDecodeError:
            data = {}
        project = data.get("project", {})
        extras |= set(project.get("optional-dependencies", {}))
        stack.requires_python = project.get("requires-python")
        stack.dependency_groups = [g for g in data.get("dependency-groups", {}) if g in TEST_EXTRA_NAMES]
        # a pyproject with only tool config (ruff, black...) isn't an installable package
        stack.installable = "build-system" in data or "project" in data
        requires = data.get("build-system", {}).get("requires")
        if isinstance(requires, list):
            stack.build_requires = [r for r in requires if isinstance(r, str)]

    if "setup.py" in root:
        stack.installable = True
    if "setup.cfg" in root:
        cfg = configparser.ConfigParser()
        try:
            cfg.read_string(read_at(repo, sha, "setup.cfg") or "")
            if cfg.has_section("options.extras_require"):
                extras |= set(cfg["options.extras_require"])
            if cfg.has_section("metadata") or cfg.has_section("options"):
                stack.installable = True
        except configparser.Error:
            pass

    stack.extras = sorted(e for e in extras if e in TEST_EXTRA_NAMES)
    if stack.installable and not stack.build_requires:
        stack.build_requires = ["setuptools>=40.8.0", "wheel"]  # pip's legacy default backend
    return stack


def install_script(stack: Stack) -> str:
    """Shell steps that get a Python repo's deps and pytest installed, run from the repo root."""
    # no wheel cache: a cached build of the project could outlive the commit it came from
    pip = "python -m pip install -q --disable-pip-version-check --no-cache-dir"
    lines = [
        "set -e",
        # git archive drops .git, so version-from-git plugins (setuptools-scm, hatch-vcs) need a hint
        'export SETUPTOOLS_SCM_PRETEND_VERSION="${SETUPTOOLS_SCM_PRETEND_VERSION:-0.0.0}"',
        f"{pip} --upgrade pip",
    ]
    for req in stack.requirements:
        lines.append(f"{pip} -r {shlex.quote(req)}")
    if stack.installable:
        if stack.build_requires:
            lines.append(f"{pip} " + " ".join(shlex.quote(r) for r in stack.build_requires))
        target = f".[{','.join(stack.extras)}]" if stack.extras else "."
        lines.append(f"{pip} -e {shlex.quote(target)}")
    for group in stack.dependency_groups:
        lines.append(f"{pip} --group {shlex.quote(group)}")
    lines.append(f"{pip} pytest")
    return "\n".join(lines)
