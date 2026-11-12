"""Thin wrappers over the host's git. We keep a full clone so we can mine history."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class Repo2EnvError(RuntimeError):
    """Base error for everything this package raises on purpose."""


class GitError(Repo2EnvError):
    pass


@dataclass(frozen=True)
class Repo:
    path: Path
    url: str
    slug: str


def default_cache() -> Path:
    return Path(os.environ.get("GAUNTLET_CACHE", Path.home() / ".cache" / "gauntlet"))


def slug_for(url: str) -> str:
    """`https://github.com/psf/requests.git` -> `psf__requests`. Local paths get a `local__` prefix."""
    cleaned = url.rstrip("/").removesuffix(".git")
    if re.match(r"^(https?://|git@|ssh://)", cleaned):
        parts = re.split(r"[/:]", cleaned)
        return f"{parts[-2]}__{parts[-1]}"
    return f"local__{Path(cleaned).name}"


def git_bytes(path: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True)
    if proc.returncode != 0:
        msg = proc.stderr.decode(errors="replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {msg}")
    return proc.stdout


def git(path: Path, *args: str) -> str:
    return git_bytes(path, *args).decode("utf-8", errors="replace")


def clone(url: str, cache_dir: Path | None = None) -> Repo:
    """Clone (or refresh) a full copy of the repo in the cache."""
    slug = slug_for(url)
    dest = (cache_dir or default_cache()) / "repos" / slug
    if (dest / ".git").exists():
        git(dest, "fetch", "--quiet", "--tags", "origin")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(["git", "clone", "--quiet", url, str(dest)], capture_output=True)
        if proc.returncode != 0:
            raise GitError(f"couldn't clone {url}: {proc.stderr.decode(errors='replace').strip()}")
    return Repo(dest, url, slug)


def resolve(repo: Repo, ref: str) -> str:
    for candidate in (ref, f"origin/{ref}"):
        try:
            return git(repo.path, "rev-parse", "--verify", f"{candidate}^{{commit}}").strip()
        except GitError:
            continue
    raise GitError(f"no such ref {ref!r} in {repo.url}")


def parent(repo: Repo, sha: str) -> str | None:
    try:
        return git(repo.path, "rev-parse", "--verify", f"{sha}^").strip()
    except GitError:
        return None


def archive(repo: Repo, sha: str) -> bytes:
    return git_bytes(repo.path, "archive", "--format=tar", sha)


def files_at(repo: Repo, sha: str) -> list[str]:
    return git(repo.path, "ls-tree", "-r", "--name-only", sha).splitlines()


def read_at(repo: Repo, sha: str, path: str) -> str | None:
    try:
        return git(repo.path, "show", f"{sha}:{path}")
    except GitError:
        return None


def changed_files(repo: Repo, a: str, b: str) -> list[str]:
    return git(repo.path, "diff", "--name-only", a, b).splitlines()


def diff(repo: Repo, a: str, b: str, paths: list[str]) -> str:
    if not paths:
        return ""
    return git(repo.path, "diff", "--binary", a, b, "--", *paths)


def history(repo: Repo, ref: str, limit: int) -> list[tuple[str, str]]:
    """Newest-first list of (sha, full message) for non-merge commits."""
    out = git(repo.path, "log", "--no-merges", f"-n{limit}", "--format=%H%x00%B%x1e", ref)
    commits = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, message = record.partition("\x00")
        commits.append((sha.strip(), message.strip()))
    return commits


_TEST_DIRS = {"test", "tests", "testing", "__tests__"}
_JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}


def _is_js_test_module(p: PurePosixPath) -> bool:
    """A JS or TS file a runner may collect tests from.

    calc.test.ts and calc.spec.js are the default names. A file inside a test folder counts
    too, whatever it is called: immer keeps its suite in `__tests__/base.js` and yaml in
    `tests/doc/anchors.ts`, each with an `include` in its vitest config to match. Reading that
    config would mean running it. Being generous here costs nothing, because a file the runner
    doesn't collect reports no tests and the PR is turned away at the next stage for that.
    """
    if p.suffix not in _JS_SUFFIXES or p.name.endswith((".d.ts", ".d.mts", ".d.cts")):
        return False
    if PurePosixPath(p.stem).suffix in (".test", ".spec"):
        return True
    return any(part in _TEST_DIRS for part in p.parts[:-1])


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    if p.name == "conftest.py" or any(part in _TEST_DIRS for part in p.parts[:-1]):
        return True
    if _is_js_test_module(p):
        return True
    return p.suffix == ".py" and (p.name.startswith("test_") or p.stem.endswith("_test"))


def is_test_module(path: str) -> bool:
    """A file the test runner would collect tests from by default."""
    p = PurePosixPath(path)
    if _is_js_test_module(p):
        return True
    return p.suffix == ".py" and (p.name.startswith("test_") or p.stem.endswith("_test"))


def is_source_path(path: str, language: str = "python") -> bool:
    """Code in the task's language that isn't a test: what a gold patch has to change."""
    if is_test_path(path):
        return False
    suffix = PurePosixPath(path).suffix
    return suffix in _JS_SUFFIXES if language == "node" else suffix == ".py"
