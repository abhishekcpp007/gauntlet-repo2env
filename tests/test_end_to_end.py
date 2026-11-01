"""The full loop on a real container: build, mine, hand a task to an 'agent', grade it."""

import docker
import pytest

from gauntlet_repo2env import build_env, evaluate, extract_patch, mine_tasks, open_task
from gauntlet_repo2env.env import UnsupportedRepo

from .conftest import _commit, _git

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from .conftest import make_calc_repo

    root = tmp_path_factory.mktemp("e2e")
    repo = make_calc_repo(root)
    cache = root / "cache"
    env = build_env(str(repo), cache_dir=cache)
    tasks = list(mine_tasks(env, cache_dir=cache))
    yield env, tasks, cache
    try:
        docker.from_env().images.remove(env.image, force=True)
    except docker.errors.APIError:
        pass


def test_env_baseline(built):
    env, _, _ = built
    assert env.image.startswith("gauntlet-env/local__calc:")
    assert sorted(env.baseline.passed) == [
        "tests/test_calc.py::test_add",
        "tests/test_calc.py::test_mul",
        "tests/test_ops.py::TestSubMore::test_zero",
        "tests/test_ops.py::test_sub",
    ]


def test_mined_tasks(built):
    _, tasks, _ = built
    by_title = {t.problem_statement.splitlines()[0]: t for t in tasks}
    assert set(by_title) == {"Fix sub argument order", "Add mul"}

    sub = by_title["Fix sub argument order"]
    # test_zero already passes against the buggy sub (0 - 0 == 0), so it guards
    # against regressions instead of proving the fix
    assert sub.fail_to_pass == ["tests/test_ops.py::test_sub"]
    assert sub.pass_to_pass == ["tests/test_ops.py::TestSubMore::test_zero"]
    assert "calc/ops.py" in sub.gold_patch and "tests/" not in sub.gold_patch

    mul = by_title["Add mul"]
    # the new test file imports mul, so before the fix the whole module fails to import
    # and test_add goes down with it
    assert mul.fail_to_pass == ["tests/test_calc.py::test_add", "tests/test_calc.py::test_mul"]
    assert mul.pass_to_pass == []


def test_gold_resolves_and_empty_does_not(built):
    _, tasks, cache = built
    for task in tasks:
        assert evaluate(task, task.gold_patch, cache_dir=cache).resolved, task.instance_id
        assert not evaluate(task, "", cache_dir=cache).resolved, task.instance_id


def test_bad_patch_reports_cleanly(built):
    _, tasks, cache = built
    res = evaluate(tasks[0], "this is not a diff\n", cache_dir=cache)
    assert not res.patch_applied and not res.resolved and res.error


def test_agent_loop(built, tmp_path):
    _, tasks, cache = built
    task = next(t for t in tasks if t.problem_statement.startswith("Add mul"))

    with open_task(task, cache_dir=cache, trajectory_path=tmp_path / "agent.jsonl") as sb:
        # the hidden test isn't there for the agent to peek at
        assert "test_mul" not in sb.read_text("tests/test_calc.py")
        code = sb.read_text("calc/__init__.py")
        sb.write_file("calc/__init__.py", code + "\n\ndef mul(a, b):\n    return a * b\n")
        patch = extract_patch(sb, task)

    assert "+def mul(a, b):" in patch
    assert evaluate(task, patch, cache_dir=cache).resolved


def test_wrong_fix_fails(built):
    _, tasks, cache = built
    task = next(t for t in tasks if t.problem_statement.startswith("Add mul"))
    with open_task(task, cache_dir=cache) as sb:
        code = sb.read_text("calc/__init__.py")
        sb.write_file("calc/__init__.py", code + "\n\ndef mul(a, b):\n    return a + b\n")
        patch = extract_patch(sb, task)
    res = evaluate(task, patch, cache_dir=cache)
    assert not res.resolved
    assert res.fail_to_pass == {"tests/test_calc.py::test_add": True, "tests/test_calc.py::test_mul": False}


def test_non_python_repo_is_refused(tmp_path):
    repo = tmp_path / "js"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "init", {"package.json": "{}"})
    with pytest.raises(UnsupportedRepo, match="node"):
        build_env(str(repo), cache_dir=tmp_path / "cache")


FORCE_PASS_CONFTEST = """\
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    report.outcome = "passed"
    report.longrepr = None
"""


def _new_file_diff(path: str, body: str) -> str:
    lines = body.splitlines()
    added = "".join(f"+{ln}\n" for ln in lines)
    head = f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
    return f"{head}@@ -0,0 +1,{len(lines)} @@\n{added}"


def test_conftest_hack_does_not_resolve(built):
    # without patch guarding this exact hack scored resolved=True on the sub task
    _, tasks, cache = built
    task = next(t for t in tasks if t.problem_statement.startswith("Fix sub"))
    res = evaluate(task, _new_file_diff("conftest.py", FORCE_PASS_CONFTEST), cache_dir=cache)
    assert not res.resolved
    assert res.ignored_files == ["conftest.py"]
    assert res.fail_to_pass == {"tests/test_ops.py::test_sub": False}


def test_test_edits_are_ignored_but_real_fix_still_counts(built):
    _, tasks, cache = built
    task = next(t for t in tasks if t.problem_statement.startswith("Add mul"))
    with open_task(task, cache_dir=cache) as sb:
        code = sb.read_text("calc/__init__.py")
        sb.write_file("calc/__init__.py", code + "\n\ndef mul(a, b):\n    return a * b\n")
        tests = sb.read_text("tests/test_calc.py")
        sb.write_file("tests/test_calc.py", tests.replace("== 5", "== 5  # checked"))
        patch = extract_patch(sb, task)
    assert "tests/test_calc.py" in patch
    res = evaluate(task, patch, cache_dir=cache)
    assert res.resolved, res.error
    assert res.ignored_files == ["tests/test_calc.py"]
