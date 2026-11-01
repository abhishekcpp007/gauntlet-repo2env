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


def _test_id(case: ET.Element) -> str:
    name = case.get("name", "?")
    classname = case.get("classname", "")
    file = case.get("file")
    if not file:
        return f"{classname}::{name}" if classname else name
    module = str(PurePosixPath(file).with_suffix("")).replace("/", ".")
    if classname.startswith(module + "."):
        return f"{file}::{classname[len(module) + 1 :]}::{name}"
    return f"{file}::{name}"


def parse_junit(xml_text: str) -> dict[str, str]:
    """Map each test id to passed, failed, error or skipped."""
    results: dict[str, str] = {}
    root = ET.fromstring(xml_text)
    for case in root.iter("testcase"):
        status = "passed"
        for child in case:
            if child.tag == "failure":
                status = "failed"
            elif child.tag == "error":
                status = "error"
            elif child.tag == "skipped":
                status = "skipped"
        results[_test_id(case)] = status
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
