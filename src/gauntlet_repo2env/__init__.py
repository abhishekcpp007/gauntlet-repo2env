"""gauntlet-repo2env: turn Python and Node repos into Docker test environments and SWE-style tasks."""

from .detect import Stack, detect, install_script
from .env import BuildFailed, EnvSpec, UnsupportedRepo, base_image_for, build_env, check_supported
from .repo import GitError, Repo2EnvError
from .tasks import (
    EvalResult,
    PatchFailed,
    Task,
    evaluate,
    extract_patch,
    is_js_runner_config,
    load_tasks,
    mine_tasks,
    open_task,
    save_tasks,
    split_patch,
    strip_test_changes,
)
from .testrun import TestReport, parse_jest_json, parse_junit, parse_report, run_tests, suite_command

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
    "base_image_for",
    "build_env",
    "check_supported",
    "detect",
    "evaluate",
    "extract_patch",
    "install_script",
    "is_js_runner_config",
    "load_tasks",
    "mine_tasks",
    "open_task",
    "parse_jest_json",
    "parse_junit",
    "parse_report",
    "run_tests",
    "save_tasks",
    "split_patch",
    "strip_test_changes",
    "suite_command",
]
__version__ = "0.1.0"
