"""Mine SWE-style tasks from git history, hand them to agents, and grade the patches that come back.

A commit becomes a task when it touches both source and tests, and at least one test fails
before the source change and passes after it. That's the same recipe SWE-bench uses, minus
the GitHub issue text (the commit message stands in as the problem statement for now).
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Iterator
from pathlib import Path

from gauntlet_sandbox import Sandbox, SandboxConfig
from pydantic import BaseModel, Field

from .env import EnvSpec, Log
from .repo import (
    Repo,
    Repo2EnvError,
    archive,
    changed_files,
    clone,
    diff,
    history,
    is_test_module,
    is_test_path,
    parent,
)
from .testrun import TestReport, run_tests

PATCH_DIR = "/tmp/gauntlet-patches"
GIT_ID = "-c user.name=gauntlet -c user.email=gauntlet@localhost"


class PatchFailed(Repo2EnvError):
    pass


class Task(BaseModel):
    format: str = "gauntlet.task/v1"
    instance_id: str
    repo: str
    image: str
    workdir: str
    base_commit: str
    solution_commit: str
    problem_statement: str
    gold_patch: str
    test_patch: str
    test_files: list[str]
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    created_at: float = Field(default_factory=time.time)


class EvalResult(BaseModel):
    instance_id: str
    resolved: bool
    patch_applied: bool
    fail_to_pass: dict[str, bool] = Field(default_factory=dict)
    pass_to_pass: dict[str, bool] = Field(default_factory=dict)
    report: TestReport | None = None
    error: str | None = None


# ---------------------------------------------------------------------- file io


def save_tasks(tasks: list[Task], path: str | Path) -> None:
    with Path(path).open("w", encoding="utf-8") as f:
        for t in tasks:
            f.write(t.model_dump_json() + "\n")


def load_tasks(path: str | Path) -> list[Task]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [Task.model_validate(json.loads(line)) for line in lines if line.strip()]


# ---------------------------------------------------------------------- helpers


def _boot(image: str, workdir: str, repo: Repo, sha: str, timeout: float, **kwargs) -> Sandbox:
    # workdir doubles as the base for relative paths, so agents can say "src/x.py"
    cfg = SandboxConfig(image=image, workdir=workdir, default_timeout=timeout, max_output_bytes=200_000)
    sb = Sandbox(cfg, **kwargs).start()
    try:
        # empty it rather than delete it, it's every exec's working directory
        sb.exec(f"find {shlex.quote(workdir)} -mindepth 1 -delete")
        sb.put_archive(workdir, archive(repo, sha))
    except Exception:
        sb.close()
        raise
    return sb


def _apply(sb: Sandbox, workdir: str, patch: str, name: str) -> None:
    if not patch.strip():
        return
    path = f"{PATCH_DIR}/{name}.diff"
    sb.write_file(path, patch if patch.endswith("\n") else patch + "\n")
    res = sb.exec(f"git apply --whitespace=nowarn {path}", workdir=workdir)
    if not res.ok:
        raise PatchFailed(f"{name} didn't apply: {res.stderr.strip()[:2000]}")


# ---------------------------------------------------------------------- mining


def mine_tasks(
    env: EnvSpec,
    *,
    max_commits: int = 200,
    limit: int = 10,
    test_timeout: float = 600,
    cache_dir: Path | None = None,
    log: Log = lambda _: None,
) -> Iterator[Task]:
    """Walk history newest first and yield every commit that makes a valid task."""
    repo = clone(env.repo, cache_dir)
    found = 0
    for sha, message in history(repo, env.commit, max_commits):
        if found >= limit:
            return
        base = parent(repo, sha)
        if base is None:
            continue
        files = changed_files(repo, base, sha)
        tests = [f for f in files if is_test_path(f)]
        source = [f for f in files if not is_test_path(f)]
        test_modules = [f for f in tests if is_test_module(f)]
        if not test_modules or not any(f.endswith(".py") for f in source):
            continue

        short = sha[:12]
        log(f"{short} candidate: {message.splitlines()[0][:70]}")
        test_patch = diff(repo, base, sha, tests)
        gold_patch = diff(repo, base, sha, source)

        try:
            sb = _boot(env.image, env.workdir, repo, base, test_timeout, record=False)
        except Repo2EnvError as e:
            log(f"{short} skipped: {e}")
            continue
        try:
            _apply(sb, env.workdir, test_patch, "test")
            present = [
                f for f in test_modules if sb.exec(f"test -f {shlex.quote(f)}", workdir=env.workdir).ok
            ]
            before = run_tests(sb, env.workdir, present, timeout=test_timeout)
            _apply(sb, env.workdir, gold_patch, "gold")
            after = run_tests(sb, env.workdir, present, timeout=test_timeout)
        except PatchFailed as e:
            log(f"{short} skipped: {e}")
            continue
        finally:
            sb.close()

        passed_before = set(before.passed)
        f2p = [t for t in after.passed if t not in passed_before]
        p2p = [t for t in after.passed if t in passed_before]
        if not f2p:
            log(f"{short} skipped: no test flips from failing to passing")
            continue

        found += 1
        log(f"{short} TASK: {len(f2p)} fail->pass, {len(p2p)} pass->pass")
        yield Task(
            instance_id=f"{env.slug}-{short}",
            repo=env.repo,
            image=env.image,
            workdir=env.workdir,
            base_commit=base,
            solution_commit=sha,
            problem_statement=message,
            gold_patch=gold_patch,
            test_patch=test_patch,
            test_files=present,
            fail_to_pass=f2p,
            pass_to_pass=p2p,
        )


# ---------------------------------------------------------------------- agents and grading


def open_task(
    task: Task,
    *,
    timeout: float = 120,
    trajectory_path: str | Path | None = None,
    cache_dir: Path | None = None,
) -> Sandbox:
    """Boot a sandbox at the task's starting point for an agent to work in.

    The hidden tests are NOT applied. The repo is committed to a scratch git history so
    `extract_patch` can diff whatever the agent changes. Close it when you're done.
    """
    repo = clone(task.repo, cache_dir)
    sb = _boot(
        task.image,
        task.workdir,
        repo,
        task.base_commit,
        timeout,
        trajectory_path=trajectory_path,
        metadata={"instance_id": task.instance_id},
    )
    res = sb.exec(f"git init -q && git add -A && git {GIT_ID} commit -q -m base", workdir=task.workdir)
    if not res.ok:
        sb.close()
        raise Repo2EnvError(f"couldn't snapshot the starting repo: {res.stderr.strip()}")
    return sb


def extract_patch(sb: Sandbox, task: Task) -> str:
    """Everything the agent changed since `open_task`, as a git diff."""
    res = sb.exec("git add -A && git diff --cached --binary", workdir=task.workdir)
    if not res.ok:
        raise Repo2EnvError(f"couldn't diff: {res.stderr.strip()}")
    if res.truncated:
        raise Repo2EnvError("patch is bigger than the sandbox output cap, raise max_output_bytes")
    return res.stdout


def evaluate(
    task: Task,
    patch: str,
    *,
    timeout: float = 600,
    trajectory_path: str | Path | None = None,
    cache_dir: Path | None = None,
) -> EvalResult:
    """Apply a candidate patch, then the hidden tests, and check every graded test passes."""
    repo = clone(task.repo, cache_dir)
    sb = _boot(
        task.image,
        task.workdir,
        repo,
        task.base_commit,
        timeout,
        trajectory_path=trajectory_path,
        record=trajectory_path is not None,
    )
    try:
        try:
            _apply(sb, task.workdir, patch, "candidate")
        except PatchFailed as e:
            return EvalResult(instance_id=task.instance_id, resolved=False, patch_applied=False, error=str(e))
        try:
            _apply(sb, task.workdir, task.test_patch, "test")
        except PatchFailed as e:
            return EvalResult(
                instance_id=task.instance_id,
                resolved=False,
                patch_applied=True,
                error=f"hidden tests clash with the patch: {e}",
            )
        report = run_tests(sb, task.workdir, task.test_files, timeout=timeout)
    finally:
        sb.close()

    passed = set(report.passed)
    f2p = {t: t in passed for t in task.fail_to_pass}
    p2p = {t: t in passed for t in task.pass_to_pass}
    return EvalResult(
        instance_id=task.instance_id,
        resolved=all(f2p.values()) and all(p2p.values()),
        patch_applied=True,
        fail_to_pass=f2p,
        pass_to_pass=p2p,
        report=report,
    )
