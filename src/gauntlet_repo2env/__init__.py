"""gauntlet-repo2env: turn Python repos into Docker test environments and SWE-style tasks."""

from .detect import Stack, detect, install_script
from .env import BuildFailed, EnvSpec, UnsupportedRepo, build_env
from .repo import GitError, Repo2EnvError
from .tasks import (
    EvalResult,
    PatchFailed,
    Task,
    evaluate,
    extract_patch,
    load_tasks,
    mine_tasks,
    open_task,
    save_tasks,
    split_patch,
    strip_test_changes,
)
from .testrun import TestReport, parse_junit, run_tests

__all__ = [
    "BuildFailed",
    "EnvSpec",
    "EvalResult",
    "GitError",
    "PatchFailed",
    "Repo2EnvError",
    "Stack",
    "Task",
    "TestReport",
    "UnsupportedRepo",
    "build_env",
    "detect",
    "evaluate",
    "extract_patch",
    "install_script",
    "load_tasks",
    "mine_tasks",
    "open_task",
    "parse_junit",
    "run_tests",
    "save_tasks",
    "split_patch",
    "strip_test_changes",
]
__version__ = "0.1.0"
