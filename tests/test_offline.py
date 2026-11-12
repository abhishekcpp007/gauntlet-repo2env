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
    assert stack.build_requires == ["setuptools>=61"]
    assert "pip install -q --disable-pip-version-check --no-cache-dir 'setuptools>=61'" in script
    assert script.index("'setuptools>=61'") < script.index("-e '.[test]'")
    assert script.rstrip().endswith("pytest")
    assert all("--no-cache-dir" in line for line in script.splitlines() if "pip install" in line)


@pytest.mark.parametrize(
    ("path", "is_req"),
    [
        ("requirements.txt", True),
        ("dev-requirements.txt", True),
        ("requirements/test.txt", True),
        ("tests/requirements.txt", True),  # tablib: pytest-cov lives here
        ("test/requirements-ci.txt", True),
        ("tests/data/requirements.txt", False),  # fixture data, not deps
        ("docs/requirements.txt", False),
        ("tests/notes.txt", False),
    ],
)
def test_requirements_files(path, is_req):
    from gauntlet_repo2env.detect import _is_requirements

    assert _is_requirements(path) is is_req


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


# ---------------------------------------------- ids from runners that are not pytest

JEST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="jest tests" tests="2" failures="1">
<testsuite name="calc" tests="2" failures="1" file="src/__tests__/calc.test.js">
<testcase classname="calc adds numbers" name="calc adds numbers" time="0.004"/>
<testcase classname="calc divides numbers" name="calc divides numbers" time="0.002">
<failure>Expected 1 but received 2</failure></testcase>
</testsuite></testsuites>"""

VITEST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="vitest tests" tests="1">
<testsuite name="src/calc.test.ts" tests="1">
<testcase classname="src/calc.test.ts" name="adds numbers" time="0.003"/>
</testsuite></testsuites>"""

NO_FILE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="1"><testsuite name="example.com/calc" tests="1">
<testcase classname="example.com/calc" name="TestAdd" time="0.001"/>
</testsuite></testsuites>"""


def test_a_suites_file_attribute_names_the_test():
    """jest-junit records the file on the testsuite, not on each testcase."""
    assert parse_junit(JEST_XML) == {
        "src/__tests__/calc.test.js::calc adds numbers": "passed",
        "src/__tests__/calc.test.js::calc divides numbers": "failed",
    }


def test_a_suite_named_after_a_file_counts_as_one():
    """vitest labels the suite with the path instead of setting `file`."""
    assert parse_junit(VITEST_XML) == {"src/calc.test.ts::adds numbers": "passed"}


def test_a_suite_name_that_is_not_a_path_is_left_alone():
    """Go packages are not files, so inventing a path from one would be a lie."""
    assert parse_junit(NO_FILE_XML) == {"example.com/calc::TestAdd": "passed"}


def test_a_repeated_classname_is_not_said_twice():
    xml = """<testsuite name="s" tests="1">
    <testcase classname="adds numbers" name="adds numbers"/></testsuite>"""
    assert parse_junit(xml) == {"adds numbers": "passed"}


def test_pytest_ids_are_untouched():
    """The ids in every existing task are pytest ids. They have to come out the same."""
    xml = """<testsuites><testsuite name="pytest" tests="2">
    <testcase classname="tests.test_calc" file="tests/test_calc.py" name="test_add"/>
    <testcase classname="tests.test_calc.TestMath" file="tests/test_calc.py" name="test_div">
    <failure message="x">boom</failure></testcase>
    </testsuite></testsuites>"""
    assert parse_junit(xml) == {
        "tests/test_calc.py::test_add": "passed",
        "tests/test_calc.py::TestMath::test_div": "failed",
    }


def _runner_dep_repo(root, name: str):
    """A setuptools-scm project called `name`, tagged v1.4.0, then one commit, then 1.5.0."""
    import subprocess

    repo = root / name
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args],
            check=True,
            capture_output=True,
        )

    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\ndynamic = ["version"]\n\n'
        '[project.optional-dependencies]\ntesting = ["pytest"]\n\n'
        '[build-system]\nrequires = ["setuptools>=45", "setuptools-scm>=6.2"]\n'
        'build-backend = "setuptools.build_meta"\n'
    )
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    git("tag", "v1.4.0")
    (repo / "README").write_text("x\n")
    git("add", "-A")
    git("commit", "-q", "-m", "between releases")
    head = ["git", "-C", str(repo), "rev-parse", "HEAD"]
    middle = subprocess.run(head, capture_output=True, text=True).stdout
    (repo / "README").write_text("y\n")
    git("commit", "-q", "-am", "release")
    git("tag", "1.5.0")
    return repo, middle.strip()


def test_pytests_own_deps_get_their_release_version(tmp_path):
    # Stamped 0.0.0, pluggy sends pip back to a pytest from 2017 that can't import on 3.12
    src, middle = _runner_dep_repo(tmp_path, "pluggy")
    repo = clone(str(src), tmp_path / "cache")
    at_tag = detect(repo, resolve(repo, "v1.4.0"))
    assert at_tag.package_name == "pluggy"
    assert at_tag.scm_version == "1.4.0"
    assert 'SETUPTOOLS_SCM_PRETEND_VERSION:-1.4.0}"' in install_script(at_tag)
    # between releases it's the release before, never the one that comes later
    assert detect(repo, middle).scm_version == "1.4.0"
    assert detect(repo, resolve(repo, "1.5.0")).scm_version == "1.5.0"


def test_other_projects_keep_the_zero_version(tmp_path, calc_repo):
    # every other env's install script, and so its fingerprint, stays what it was
    src, _ = _runner_dep_repo(tmp_path, "my_lib")
    repo = clone(str(src), tmp_path / "cache")
    stack = detect(repo, resolve(repo, "v1.4.0"))
    assert stack.package_name == "my_lib" and stack.scm_version is None
    assert 'SETUPTOOLS_SCM_PRETEND_VERSION:-0.0.0}"' in install_script(stack)
    calc = clone(str(calc_repo), tmp_path / "cache2")
    assert 'SETUPTOOLS_SCM_PRETEND_VERSION:-0.0.0}"' in install_script(detect(calc, resolve(calc, "HEAD")))


def test_a_runner_dep_with_a_static_version_is_left_alone(tmp_path):
    # tomli and packaging build with flit, so the pretend version never reaches them
    src, _ = _runner_dep_repo(tmp_path, "packaging")
    py = src / "pyproject.toml"
    py.write_text(py.read_text().replace('"setuptools>=45", "setuptools-scm>=6.2"', '"flit_core>=3.2"'))
    import subprocess

    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", *ident, "-C", str(src), "commit", "-qam", "flit"], check=True)
    repo = clone(str(src), tmp_path / "cache")
    assert detect(repo, resolve(repo, "HEAD")).scm_version is None
