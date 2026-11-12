"""Build a reusable Docker image with a repo and all its test deps installed."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path

from gauntlet_sandbox import Sandbox, SandboxConfig
from pydantic import BaseModel

from .detect import Stack, detect, install_script
from .repo import Repo2EnvError, archive, clone, resolve
from .testrun import TestReport, run_tests

WORKDIR = "/workspace/repo"
ENV_REPO = "gauntlet-env"

Log = Callable[[str], None]


class UnsupportedRepo(Repo2EnvError):
    pass


class BuildFailed(Repo2EnvError):
    pass


class EnvSpec(BaseModel):
    format: str = "gauntlet.env/v1"
    repo: str
    slug: str
    commit: str
    image: str
    base_image: str
    workdir: str = WORKDIR
    stack: Stack
    install_script: str
    baseline: TestReport
    built_at: float

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2) + "\n")

    @classmethod
    def load(cls, path: str | Path) -> EnvSpec:
        return cls.model_validate(json.loads(Path(path).read_text()))


def check_supported(slug: str, stack: Stack) -> None:
    """Raise UnsupportedRepo unless this is a stack an env can be built for."""
    if stack.language == "python":
        return
    if stack.language == "node":
        if stack.unsupported:
            raise UnsupportedRepo(f"{slug} is a node project, but {stack.unsupported}")
        return
    raise UnsupportedRepo(f"{slug} looks like a {stack.language} project, only Python and Node are handled")


def base_image_for(stack: Stack, python: str = "3.12") -> str:
    if stack.language == "node":
        return f"node:{stack.node or '22'}-slim"
    return f"python:{python}-slim"


def _image_name(slug: str) -> str:
    return f"{ENV_REPO}/" + re.sub(r"[^a-z0-9._-]+", "-", slug.lower()).strip("-.")


def build_env(
    url: str,
    *,
    ref: str = "HEAD",
    python: str = "3.12",
    apt_packages: tuple[str, ...] = ("git",),
    install_timeout: float = 1800,
    test_timeout: float = 1800,
    baseline_tests: bool = True,
    cache_dir: Path | None = None,
    log: Log = lambda _: None,
) -> EnvSpec:
    """Clone `url`, install it in a fresh container, run its tests once, and freeze the result."""
    log(f"cloning {url}")
    repo = clone(url, cache_dir)
    sha = resolve(repo, ref)
    stack = detect(repo, sha)
    check_supported(repo.slug, stack)

    script = install_script(stack)
    base_image = base_image_for(stack, python)
    cfg = SandboxConfig(
        image=base_image,
        network=True,  # only during the build, task sandboxes run offline
        drop_capabilities=False,  # apt-get needs chown and friends
        memory="4g",
        cpus=2,
        default_timeout=install_timeout,
        max_output_bytes=200_000,
    )

    with Sandbox(cfg, record=False) as sb:
        if apt_packages:
            log(f"installing system packages: {' '.join(apt_packages)}")
            res = sb.exec(
                "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                f"--no-install-recommends {' '.join(apt_packages)} >/dev/null"
            )
            if not res.ok:
                raise BuildFailed(f"apt-get failed:\n{res.stderr[-3000:]}")

        log(f"copying {repo.slug} @ {sha[:12]}")
        sb.put_archive(WORKDIR, archive(repo, sha))

        log(f"installing {stack.language} deps")
        res = sb.exec(script, workdir=WORKDIR)
        if not res.ok:
            raise BuildFailed(f"dependency install failed:\n{(res.stdout + res.stderr)[-3000:]}")

        if baseline_tests:
            log("running the test suite once for a baseline")
            baseline = run_tests(sb, WORKDIR, timeout=test_timeout, runner=stack.runner)
            log(f"baseline: {baseline.summary()}")
        else:
            baseline = TestReport()

        # leave nothing behind that a later task sandbox could learn from: grader scratch
        # files, pip, npm and apt caches
        sb.exec("rm -rf /tmp/gauntlet-* /root/.cache /root/.npm /var/lib/apt/lists/*")

        snap = sb.snapshot(label="env")
        image = sb.client.images.get(snap)
        name, tag = _image_name(repo.slug), sha[:12]
        image.tag(name, tag)
        sb.client.images.remove(snap)  # drop the temporary tag, the image stays

    return EnvSpec(
        repo=url,
        slug=repo.slug,
        commit=sha,
        image=f"{name}:{tag}",
        base_image=base_image,
        stack=stack,
        install_script=script,
        baseline=baseline,
        built_at=time.time(),
    )
