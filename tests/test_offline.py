"""Fast tests that need git but not Docker."""

import pytest

from gauntlet_repo2env import detect, install_script, parse_junit
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
