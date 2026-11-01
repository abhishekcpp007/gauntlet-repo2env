"""Fast tests that need git but not Docker."""

import pytest

from gauntlet_repo2env import detect, install_script, parse_junit, split_patch, strip_test_changes
from gauntlet_repo2env.repo import clone, history, is_test_module, is_test_path, resolve, slug_for


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://github.com/psf/requests", "psf__requests"),
        ("https://github.com/psf/requests.git", "psf__requests"),
        ("git@github.com:pallets/click.git", "pallets__click"),
        ("/home/me/code/calc/", "local__calc"),
    ],
)
def test_slug(url, slug):
    assert slug_for(url) == slug


@pytest.mark.parametrize(
    "path,test,module",
    [
        ("tests/test_x.py", True, True),
        ("src/pkg/x_test.py", True, True),
        ("tests/helpers.py", True, False),
        ("conftest.py", True, False),
        ("pkg/core.py", False, False),
        ("pkg/testing_utils.py", False, False),
        ("README.md", False, False),
    ],
)
def test_test_paths(path, test, module):
    assert is_test_path(path) is test
    assert is_test_module(path) is module


def test_detect_python(calc_repo, tmp_path):
    repo = clone(str(calc_repo), tmp_path / "cache")
    stack = detect(repo, resolve(repo, "HEAD"))
    assert stack.language == "python"
    assert stack.installable
    assert stack.extras == ["test"]  # docs is ignored
    script = install_script(stack)
    assert "-e '.[test]'" in script
    assert script.rstrip().endswith("pytest")


def test_history(calc_repo, tmp_path):
    repo = clone(str(calc_repo), tmp_path / "cache")
    msgs = [m.splitlines()[0] for _, m in history(repo, "HEAD", 10)]
    assert msgs[0] == "Fix sub argument order"
    assert msgs[-1] == "Initial calc with add"


def test_parse_junit():
    xml = """<testsuites><testsuite>
      <testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py"/>
      <testcase classname="tests.test_a.TestK" name="test_m" file="tests/test_a.py"><failure/></testcase>
      <testcase classname="tests.test_b" name="test_e" file="tests/test_b.py"><error/></testcase>
      <testcase classname="tests.test_b" name="test_s" file="tests/test_b.py"><skipped/></testcase>
    </testsuite></testsuites>"""
    assert parse_junit(xml) == {
        "tests/test_a.py::test_ok": "passed",
        "tests/test_a.py::TestK::test_m": "failed",
        "tests/test_b.py::test_e": "error",
        "tests/test_b.py::test_s": "skipped",
    }


HACK = (
    "diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n--- /dev/null\n+++ b/conftest.py\n"
    "@@ -0,0 +1 @@\n+collect_ignore = ['tests']\n"
)
SRC = "diff --git a/calc/ops.py b/calc/ops.py\n--- a/calc/ops.py\n+++ b/calc/ops.py\n@@ -1 +1 @@\n-x\n+y\n"
INI = "diff --git a/tox.ini b/tox.ini\n--- a/tox.ini\n+++ b/tox.ini\n@@ -1 +1 @@\n-a\n+b\n"
RENAME = (
    "diff --git a/tests/test_a.py b/old_a.py\nsimilarity index 100%\n"
    "rename from tests/test_a.py\nrename to old_a.py\n"
)


def _task(**kw):
    from gauntlet_repo2env import Task

    base = dict(
        instance_id="x",
        repo="r",
        image="i",
        workdir="/w",
        base_commit="a",
        solution_commit="b",
        problem_statement="p",
        gold_patch="",
        test_patch="",
        test_files=["tests/test_ops.py"],
        fail_to_pass=[],
        pass_to_pass=[],
    )
    return Task(**{**base, **kw})


def test_split_patch():
    sections = split_patch(SRC + HACK)
    assert [paths for paths, _ in sections] == [["calc/ops.py"], ["conftest.py"]]
    assert "".join(text for _, text in sections) == SRC + HACK


def test_strip_test_changes_keeps_source_only():
    kept, ignored = strip_test_changes(HACK + SRC + INI + RENAME, _task())
    assert kept == SRC
    assert ignored == ["conftest.py", "tests/test_a.py", "tox.ini"]


def test_strip_test_changes_leaves_clean_patches_alone():
    assert strip_test_changes(SRC, _task()) == (SRC, [])
    assert strip_test_changes("", _task()) == ("", [])
    assert strip_test_changes("not a diff\n", _task()) == ("not a diff\n", [])
