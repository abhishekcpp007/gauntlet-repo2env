"""Run pytest inside a sandbox and turn its JUnit XML into per-test results."""

from __future__ import annotations

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


def run_tests(
    sb: Sandbox, workdir: str, test_files: list[str] | None = None, *, timeout: float = 600
) -> TestReport:
    targets = " ".join(shlex.quote(f) for f in test_files or [])
    cmd = (
        f"rm -f {JUNIT_PATH}; python -m pytest -p no:cacheprovider -q -o junit_family=xunit1 "
        f"--junitxml={JUNIT_PATH} {targets}"
    )
    res = sb.exec(cmd, workdir=workdir, timeout=timeout)
    tail = (res.stdout + res.stderr)[-4000:]
    report = TestReport(exit_code=res.exit_code, duration=res.duration, output_tail=tail)
    try:
        xml_text = sb.read_text(JUNIT_PATH)
    except FileNotFoundError:
        return report  # pytest died before writing results, the tail says why

    buckets = {"passed": report.passed, "failed": report.failed, "error": report.errors}
    buckets["skipped"] = report.skipped
    for test_id, status in sorted(parse_junit(xml_text).items()):
        buckets[status].append(test_id)
    return report
