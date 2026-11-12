"""Work out what kind of project a repo is and how to install it."""

from __future__ import annotations

import configparser
import fnmatch
import json
import re
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


DEFAULT_NODE = "22"
NODE_FLOOR = 18  # the oldest node image that still builds, see _detect_node
# newest first: a lockfile says which package manager wrote the tree that was tested upstream
LOCKFILES = {"pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "package-lock.json": "npm"}


class Stack(BaseModel):
    language: str
    # which test runner reports results. Everything downstream (the suite command, test ids,
    # the canary) is chosen by this and not by the language, since one language has several.
    runner: str = "pytest"
    # set when the repo was recognised but can't be built yet, with the reason in plain words
    unsupported: str | None = None
    # node only
    package_manager: str | None = None
    package_manager_version: str | None = None
    lockfile: str | None = None
    node: str | None = None
    package_name: str | None = None
    installable: bool = False
    extras: list[str] = Field(default_factory=list)
    dependency_groups: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    requires_python: str | None = None
    # the PEP 517 build backend, installed into the env so the project can be reinstalled
    # offline later (a task sandbox at an older commit has to regenerate files like _version.py)
    build_requires: list[str] = Field(default_factory=list)


# Folders whose requirements files are the test suite's own deps. tablib keeps pytest-cov in
# tests/requirements.txt and its pytest.ini passes --cov, so without it pytest exits on a usage
# error before it writes a report and every task from the repo comes back with no results.
TEST_REQUIREMENT_DIRS = ("tests", "test", "testing")


def _is_requirements(path: str) -> bool:
    p = PurePosixPath(path)
    if len(p.parts) == 1:
        return fnmatch.fnmatch(p.name, "*requirements*.txt")
    if len(p.parts) == 2 and p.parts[0] in TEST_REQUIREMENT_DIRS:
        return fnmatch.fnmatch(p.name, "*requirements*.txt")
    return len(p.parts) == 2 and p.parts[0] == "requirements" and p.suffix == ".txt"


def detect(repo: Repo, sha: str) -> Stack:
    files = files_at(repo, sha)
    root = {f for f in files if "/" not in f}

    has_py_project = bool(root & {"pyproject.toml", "setup.py", "setup.cfg"})
    requirements = sorted(f for f in files if _is_requirements(f))
    if not has_py_project and not requirements:
        language = next((lang for marker, lang in MARKERS.items() if marker in root), "unknown")
        if language == "node":
            return _detect_node(repo, sha, files, root)
        return Stack(language=language, runner="unknown")

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


def _node_major(spec: str | None) -> str | None:
    """The lowest major version an `engines.node` range or an .nvmrc line allows, if it names one.

    The lowest, because that's the version the project promises to work on. `>=18` gives 18,
    `^20.19.0 || >=22.12.0` gives 20, `v22.3.0` gives 22, `lts/*` gives nothing.
    """
    majors = [int(m) for m in re.findall(r"(?<![\d.])v?(\d{1,2})(?:\.(?:\d+|x|\*))*", spec or "")]
    majors = [m for m in majors if m >= 12]
    return str(min(majors)) if majors else None


def _pnpm_packages(text: str) -> list[str]:
    """The package globs a pnpm-workspace.yaml really lists, exclusions left out.

    The file's presence says nothing by itself. Newer pnpm keeps plain settings in it
    (`allowBuilds`, `minimumReleaseAge`, catalogs), so most single-package pnpm repos have one,
    and some carry `packages: []` or only a `!vendor/*` exclusion. Read by hand because it is
    one top-level key and not worth a YAML dependency.
    """
    found: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.split(" #")[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace() and not line.startswith("-"):
            inside = line.startswith("packages:")
            inline = line.partition(":")[2].strip() if inside else ""
            if inline.startswith("["):
                found += [x.strip().strip("'\"") for x in inline.strip("[]").split(",") if x.strip()]
            continue
        if inside and line.lstrip().startswith("-"):
            found.append(line.lstrip()[1:].strip().strip("'\""))
    return [g for g in found if g and not g.startswith("!")]


def _detect_node(repo: Repo, sha: str, files: list[str], root: set[str]) -> Stack:
    stack = Stack(language="node", runner="unknown")
    try:
        pkg = json.loads(read_at(repo, sha, "package.json") or "{}")
    except json.JSONDecodeError:
        pkg = {}
    if not isinstance(pkg, dict):
        pkg = {}
    stack.package_name = pkg.get("name") if isinstance(pkg.get("name"), str) else None

    deps: dict = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        if isinstance(pkg.get(key), dict):
            deps.update(pkg[key])
    if "vitest" in deps:
        stack.runner = "vitest"
    elif "jest" in deps:
        stack.runner = "jest"

    stack.lockfile = next((name for name in LOCKFILES if name in root), None)
    stack.package_manager = LOCKFILES.get(stack.lockfile or "", "npm")
    declared = pkg.get("packageManager")
    if isinstance(declared, str) and "@" in declared:
        name, _, version = declared.partition("@")
        if name in ("npm", "pnpm", "yarn"):
            stack.package_manager = name
            stack.package_manager_version = version.split("+")[0]  # drop the integrity hash

    engines = pkg.get("engines") if isinstance(pkg.get("engines"), dict) else {}
    pinned = next((read_at(repo, sha, f) for f in (".nvmrc", ".node-version") if f in root), None)
    asked = _node_major(pinned) or _node_major(engines.get("node")) or DEFAULT_NODE
    # `engines: >=16` is a promise about the oldest node that works, not a request for it. The
    # node:16 image is Debian buster, whose package repositories are gone, so apt-get fails and
    # the env can't be built at all. Seven radashi PRs were lost to that before this floor.
    stack.node = str(max(int(asked), NODE_FLOOR))

    # In a workspace the tests of one package resolve its siblings through symlinks the package
    # manager writes into node_modules, and the env keeps node_modules outside the repo. Saying
    # no here is better than building an env whose imports quietly resolve to the wrong copy.
    workspace = "pnpm-workspace.yaml" in root and _pnpm_packages(
        read_at(repo, sha, "pnpm-workspace.yaml") or ""
    )
    if pkg.get("workspaces") or workspace:
        stack.unsupported = "it is a workspace (monorepo), only single-package repos are handled"
    elif {"bun.lockb", "bun.lock"} & root and not stack.lockfile:
        stack.unsupported = "it is locked with bun, only npm, pnpm and yarn are handled"
    elif stack.runner == "jest":
        stack.unsupported = "its tests run on jest, only vitest is handled so far"
    elif stack.runner != "vitest":
        stack.unsupported = "no supported test runner in package.json (looked for vitest)"
    stack.installable = stack.unsupported is None
    return stack


# Where node_modules lives once the env is built: next to the repo, not inside it. A task
# sandbox empties the repo folder and unpacks the base commit into it, which would take
# node_modules with it. Node looks for packages in every parent folder, so one level up
# resolves the same. It also means nothing a patch does can reach the dependencies.
NODE_MODULES_SCRIPT = (
    "rm -rf ../node_modules && mv node_modules ../node_modules && ln -s ../node_modules node_modules"
)


def _node_install_script(stack: Stack) -> str:
    lines = [
        "set -e",
        "export CI=1 npm_config_fund=false npm_config_audit=false npm_config_update_notifier=false",
    ]
    pm = stack.package_manager or "npm"
    if pm in ("pnpm", "yarn"):
        # corepack ships with node and fetches the exact version the repo declares
        lines.append("corepack enable")
        if stack.package_manager_version:
            lines.append(f"corepack prepare {pm}@{shlex.quote(stack.package_manager_version)} --activate")
    if pm == "pnpm":
        lines.append("pnpm install --frozen-lockfile" if stack.lockfile else "pnpm install")
    elif pm == "yarn":
        # yarn 1 and yarn 2+ spell "don't touch the lockfile" differently
        lines.append("yarn install --immutable || yarn install --frozen-lockfile")
    else:
        lines.append("npm ci" if stack.lockfile else "npm install")
    lines.append(NODE_MODULES_SCRIPT)
    return "\n".join(lines)


def install_script(stack: Stack) -> str:
    """Shell steps that get a repo's deps and its test runner installed, run from the repo root."""
    if stack.language == "node":
        return _node_install_script(stack)
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
