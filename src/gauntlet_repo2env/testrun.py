"""Run a repo's tests inside a sandbox and turn the runner's report into per-test results."""

from __future__ import annotations

import json
import posixpath
import shlex
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

from gauntlet_sandbox import Sandbox
from pydantic import BaseModel, Field

JUNIT_PATH = "/tmp/gauntlet-junit.xml"


class TestReport(BaseModel):
    __test__ = False  # keep pytest from collecting this class

    passed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    exit_code: int = 0
    duration: float = 0.0
    output_tail: str = ""

    def summary(self) -> str:
        return (
            f"{len(self.passed)} passed, {len(self.failed)} failed, "
            f"{len(self.errors)} errors, {len(self.skipped)} skipped"
        )


_SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".rb", ".java")


def _looks_like_a_path(value: str | None) -> str | None:
    """A suite name that is really a file path, which is how vitest and others label a suite."""
    if value and value.endswith(_SOURCE_SUFFIXES):
        return value
    return None


def _test_id(case: ET.Element, suite_file: str | None = None) -> str:
    name = case.get("name", "?")
    classname = case.get("classname", "")
    # pytest puts `file` on the testcase. jest-junit puts it on the parent testsuite, and
    # vitest uses the suite's name for it, so fall back to whatever the suite knows.
    file = case.get("file") or suite_file
    if not file:
        # some runners repeat the test name as the classname, which would say it twice
        return name if not classname or classname == name else f"{classname}::{name}"
    module = str(PurePosixPath(file).with_suffix("")).replace("/", ".")
    if classname.startswith(module + "."):
        return f"{file}::{classname[len(module) + 1 :]}::{name}"
    return f"{file}::{name}"


def parse_junit(xml_text: str) -> dict[str, str]:
    """Map each test id to passed, failed, error or skipped."""
    results: dict[str, str] = {}
    root = ET.fromstring(xml_text)
    # walk suite by suite rather than flattening, because a testcase element has no parent
    # pointer and the file it came from is often recorded on the suite
    suites = list(root.iter("testsuite")) or [root]
    for suite in suites:
        suite_file = suite.get("file") or _looks_like_a_path(suite.get("name"))
        for case in suite.findall("testcase"):
            status = "passed"
            for child in case:
                if child.tag == "failure":
                    status = "failed"
                elif child.tag == "error":
                    status = "error"
                elif child.tag == "skipped":
                    status = "skipped"
            results[_test_id(case, suite_file)] = status
    return results


_JEST_STATUS = {"passed": "passed", "failed": "failed"}  # pending, skipped, todo, disabled: skipped


def parse_jest_json(text: str, root: str | None = None) -> dict[str, str]:
    """The same map from jest's own `--json` report.

    jest has no JUnit reporter built in, and `jest-junit` is a package most repos don't have.
    The JSON report is always there. Ids come out in the shape vitest's do,
    `path::outer describe > inner describe > title`, so one parser of test files serves both.
    jest names files by absolute path, and `root` (the repo folder) is cut off the front.
    """
    results: dict[str, str] = {}
    data = json.loads(text)
    for suite in data.get("testResults") or []:
        file = str(suite.get("name") or "?")
        if root and file.startswith(root.rstrip("/") + "/"):
            file = posixpath.relpath(file, root)
        cases = suite.get("assertionResults") or []
        if not cases and suite.get("status") == "failed":
            # the file didn't load, or isn't there: one failed entry under the file's own name,
            # which is how vitest reports it too
            results[f"{file}::{file}"] = "failed"
        for case in cases:
            name = " > ".join([*(case.get("ancestorTitles") or []), str(case.get("title", "?"))])
            results[f"{file}::{name}"] = _JEST_STATUS.get(case.get("status"), "skipped")
    return results


def parse_report(text: str, root: str | None = None) -> dict[str, str]:
    """Per-test results from whatever the runner wrote: JUnit XML, or jest's JSON."""
    if text.lstrip().startswith("{"):
        return parse_jest_json(text, root)
    return parse_junit(text)


RUNNERS = ("pytest", "vitest", "jest")


def suite_command(runner: str, junit_path: str, targets: list[str], *, keep_going: bool = False) -> str:
    """The shell command that runs `targets` (or everything) and writes its report to `junit_path`.

    One place, because the env build, task validation and grading all have to run a suite the
    same way or a test id stops meaning the same thing between them.
    """
    listing = " ".join(shlex.quote(t) for t in targets)
    if runner == "pytest":
        extra = "--continue-on-collection-errors " if keep_going else ""
        return (
            f"rm -f {junit_path}; python -m pytest -p no:cacheprovider -q -o junit_family=xunit1 "
            f"{extra}--junitxml={junit_path} {listing}"
        )
    if runner == "vitest":
        # Called by path, not through npx or a package.json script: node_modules sits one level
        # above the repo (see detect.NODE_MODULES_SCRIPT), and a script the repo defines is the
        # repo's to change. `run` is one pass with no watch mode; a file that fails to load is
        # reported as a failed suite and the rest still run.
        return (
            f"rm -f {junit_path}; CI=1 node ../node_modules/vitest/vitest.mjs run "
            f"--reporter=junit --outputFile={junit_path} {listing}"
        )
    if runner == "jest":
        # By path for the same reasons as vitest. `--ci` keeps a run from writing a snapshot
        # that isn't there, so a missing one fails. `--runTestsByPath` takes the targets as
        # files and not as patterns to search with.
        #
        # The vm-modules flag is only set when the repo's own scripts set it. It is not
        # harmless: luxon is `"type": "module"` and has babel turn its files into CommonJS,
        # and with the flag on jest loaded them as ES modules instead. All 58 of its test
        # files failed with "Must use import to load ES Module".
        by_path = f"--runTestsByPath {listing}" if targets else ""
        esm = "$(grep -q experimental-vm-modules package.json 2>/dev/null && echo --experimental-vm-modules)"
        return (
            f'rm -f {junit_path}; CI=1 NODE_OPTIONS="{esm}" '
            f"node ../node_modules/jest/bin/jest.js --ci --json --outputFile={junit_path} {by_path}"
        ).rstrip()
    raise ValueError(f"no suite command for test runner {runner!r}")


def run_tests(
    sb: Sandbox,
    workdir: str,
    test_files: list[str] | None = None,
    *,
    timeout: float = 600,
    runner: str = "pytest",
) -> TestReport:
    cmd = suite_command(runner, JUNIT_PATH, test_files or [])
    res = sb.exec(cmd, workdir=workdir, timeout=timeout)
    tail = (res.stdout + res.stderr)[-4000:]
    report = TestReport(exit_code=res.exit_code, duration=res.duration, output_tail=tail)
    try:
        xml_text = sb.read_text(JUNIT_PATH)
    except FileNotFoundError:
        return report  # the runner died before writing results, the tail says why

    buckets = {"passed": report.passed, "failed": report.failed, "error": report.errors}
    buckets["skipped"] = report.skipped
    for test_id, status in sorted(parse_report(xml_text, workdir).items()):
        buckets[status].append(test_id)
    return report
