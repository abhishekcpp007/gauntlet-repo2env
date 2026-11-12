"""Node repos: what gets detected, what gets refused, and one real env build."""

import json
from pathlib import Path

import pytest

from gauntlet_repo2env import (
    UnsupportedRepo,
    build_env,
    detect,
    install_script,
    strip_test_changes,
    suite_command,
)
from gauntlet_repo2env.detect import _node_major, _pnpm_packages
from gauntlet_repo2env.env import base_image_for, check_supported
from gauntlet_repo2env.repo import clone, is_source_path, is_test_module, is_test_path, resolve

from .conftest import _commit, _git

CALC = "export function add(a, b) {\n  return a + b\n}\n"
CALC_TEST = """\
import { describe, expect, it } from 'vitest'
import { add } from '../src/calc.js'

describe('calc', () => {
  it('adds two numbers', () => {
    expect(add(2, 3)).toBe(5)
  })
})
"""


def _package(**extra) -> str:
    pkg = {"name": "jscalc", "version": "0.1.0", "type": "module", "devDependencies": {"vitest": "^3.2.0"}}
    pkg.update(extra)
    return json.dumps(pkg, indent=2) + "\n"


def make_js_repo(root: Path, files: dict[str, str] | None = None) -> Path:
    repo = root / "jscalc"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    base = {
        "package.json": _package(),
        ".gitignore": "node_modules/\n",
        "src/calc.js": CALC,
        "test/calc.test.js": CALC_TEST,
    }
    _commit(repo, "Initial calc", {**base, **(files or {})})
    return repo


def _stack(tmp_path, files=None):
    repo = clone(str(make_js_repo(tmp_path, files)), tmp_path / "cache")
    return detect(repo, resolve(repo, "HEAD"))


def test_detect_reads_runner_manager_and_node_version(tmp_path):
    stack = _stack(tmp_path, {"package.json": _package(engines={"node": ">=20"}), "pnpm-lock.yaml": "x\n"})
    assert (stack.language, stack.runner, stack.package_manager, stack.node) == (
        "node",
        "vitest",
        "pnpm",
        "20",
    )
    assert stack.package_name == "jscalc"
    assert stack.unsupported is None and stack.installable


def test_the_declared_package_manager_wins_over_the_lockfile(tmp_path):
    stack = _stack(tmp_path, {"package.json": _package(packageManager="pnpm@9.12.0+sha512.abc")})
    assert (stack.package_manager, stack.package_manager_version) == ("pnpm", "9.12.0")
    assert "corepack prepare pnpm@9.12.0 --activate" in install_script(stack)


def test_nvmrc_beats_engines(tmp_path):
    stack = _stack(tmp_path, {"package.json": _package(engines={"node": ">=18"}), ".nvmrc": "v22.3.0\n"})
    assert stack.node == "22"
    assert base_image_for(stack) == "node:22-slim"


@pytest.mark.parametrize(
    "spec, major",
    [
        (">=18", "18"),
        ("^20.19.0 || >=22.12.0", "20"),
        ("v22.3.0", "22"),
        ("20.x", "20"),
        ("lts/*", None),
        (None, None),
        (">=0.10", None),  # ancient ranges name no version worth building on
    ],
)
def test_node_major(spec, major):
    assert _node_major(spec) == major


@pytest.mark.parametrize(
    "files, reason",
    [
        ({"package.json": _package(workspaces=["packages/*"])}, "workspace"),
        ({"pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n"}, "workspace"),
        ({"package.json": _package(devDependencies={"jest": "^29"})}, "jest"),
        ({"package.json": _package(devDependencies={"mocha": "^10"})}, "no supported test runner"),
        ({"bun.lockb": "x"}, "bun"),
    ],
)
def test_repos_it_cannot_build_say_why(tmp_path, files, reason):
    stack = _stack(tmp_path, files)
    assert stack.language == "node" and reason in stack.unsupported
    with pytest.raises(UnsupportedRepo, match=reason):
        check_supported("acme/jscalc", stack)


def test_install_keeps_node_modules_outside_the_repo(tmp_path):
    script = install_script(_stack(tmp_path, {"package-lock.json": "{}\n"}))
    assert "npm ci" in script
    # a task sandbox empties the repo folder, so the deps have to live one level up
    assert "mv node_modules ../node_modules" in script


def test_a_python_stack_is_unchanged(calc_repo, tmp_path):
    repo = clone(str(calc_repo), tmp_path / "cache")
    stack = detect(repo, resolve(repo, "HEAD"))
    assert (stack.language, stack.runner) == ("python", "pytest")
    assert "pytest" in install_script(stack) and "npm" not in install_script(stack)


@pytest.mark.parametrize(
    "path, test, module, source",
    [
        ("src/calc.test.ts", True, True, False),
        ("src/calc.spec.js", True, True, False),
        ("src/__tests__/calc.ts", True, False, False),
        ("test/helpers.ts", True, False, False),
        ("src/calc.ts", False, False, True),
        ("src/contest.ts", False, False, True),  # "test" inside a word is not a test
        ("src/latest.js", False, False, True),
        ("README.md", False, False, False),
    ],
)
def test_js_test_paths(path, test, module, source):
    assert is_test_path(path) is test
    assert is_test_module(path) is module
    assert is_source_path(path, "node") is source


def test_python_source_paths_still_mean_python():
    assert is_source_path("calc/core.py") and not is_source_path("src/calc.ts")
    assert not is_source_path("tests/test_core.py")


def test_the_vitest_command_does_not_go_through_the_repo(tmp_path):
    cmd = suite_command("vitest", "/tmp/j.xml", ["test/calc.test.js"])
    # a package.json script or a local bin shim would be the repo's to rewrite
    assert "npm" not in cmd and "npx" not in cmd
    assert "node ../node_modules/vitest/vitest.mjs run" in cmd and "--outputFile=/tmp/j.xml" in cmd
    with pytest.raises(ValueError):
        suite_command("mocha", "/tmp/j.xml", [])


def test_the_pytest_command_is_what_it_always_was():
    assert suite_command("pytest", "/tmp/gauntlet-junit.xml", ["tests/test_a.py"]) == (
        "rm -f /tmp/gauntlet-junit.xml; python -m pytest -p no:cacheprovider -q -o junit_family=xunit1 "
        "--junitxml=/tmp/gauntlet-junit.xml tests/test_a.py"
    )


def _diff(path: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n"


def test_a_patch_cannot_touch_the_runner_or_its_config():
    from gauntlet_repo2env import Task

    task = Task(
        instance_id="x", repo="r", image="i", workdir="/w", base_commit="a", solution_commit="b",
        problem_statement="", gold_patch="", test_patch="", test_files=[], fail_to_pass=[], pass_to_pass=[],
    )  # fmt: skip
    owned = [
        "vitest.config.ts",
        "vite.config.mjs",
        "vitest.setup.ts",
        "node_modules/vitest/dist/index.js",
        "src/node_modules/vitest/index.js",
        "src/calc.test.ts",
    ]
    patch = "".join(_diff(p) for p in [*owned, "src/calc.ts", "package.json"])
    kept, ignored = strip_test_changes(patch, task)
    assert ignored == sorted(owned)
    assert "src/calc.ts" in kept and "package.json" in kept


@pytest.mark.docker
def test_build_a_node_env_and_run_its_suite(tmp_path):
    spec = build_env(str(make_js_repo(tmp_path)), cache_dir=tmp_path / "cache")
    assert spec.base_image == "node:22-slim" and spec.stack.runner == "vitest"
    assert spec.baseline.passed == ["test/calc.test.js::calc > adds two numbers"]
    assert not spec.baseline.failed and not spec.baseline.errors


@pytest.mark.parametrize(
    "text, packages",
    [
        ("packages:\n  - 'packages/*'\n  - docs\n", ["packages/*", "docs"]),
        ("packages: ['packages/*', \"apps/*\"]\n", ["packages/*", "apps/*"]),
        ("packages: []\n", []),
        ('packages:\n  - "!vendor/*"\n', []),  # an exclusion alone names no package
        ("allowBuilds:\n  esbuild: true\nminimumReleaseAge: 1440\n", []),
        ("catalog:\n  vitest: ^4.1.10\ncatalogs:\n  dev:\n    - x\n", []),
        ("# packages:\n#   - 'packages/*'\n", []),
        ("packages:\n  - playground # the demo\nonlyBuiltDependencies:\n  - esbuild\n", ["playground"]),
        ("", []),
    ],
)
def test_pnpm_workspace_files_are_read_not_just_noticed(text, packages):
    assert _pnpm_packages(text) == packages


def test_a_pnpm_settings_file_is_not_a_workspace(tmp_path):
    stack = _stack(
        tmp_path, {"pnpm-workspace.yaml": "allowBuilds:\n  esbuild: true\n", "pnpm-lock.yaml": "x\n"}
    )
    assert stack.unsupported is None and stack.package_manager == "pnpm"
