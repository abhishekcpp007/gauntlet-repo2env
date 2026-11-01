"""Command line: `repo2env build|mine|list|validate|eval`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .env import EnvSpec, build_env
from .repo import Repo2EnvError
from .tasks import evaluate, load_tasks, mine_tasks


def _log(msg: str) -> None:
    print(f"  {msg}", file=sys.stderr, flush=True)


def cmd_build(args: argparse.Namespace) -> int:
    env = build_env(args.repo, ref=args.ref, python=args.python, log=_log)
    out = Path(args.output or f"{env.slug}.env.json")
    env.save(out)
    print(f"env image {env.image}")
    print(f"baseline  {env.baseline.summary()}")
    print(f"saved     {out}")
    return 0


def cmd_mine(args: argparse.Namespace) -> int:
    env = EnvSpec.load(args.env)
    out = Path(args.output or f"{env.slug}.tasks.jsonl")
    count = 0
    # write as we go so a long run that gets interrupted still leaves results
    with out.open("w", encoding="utf-8") as f:
        for task in mine_tasks(env, max_commits=args.max_commits, limit=args.limit, log=_log):
            f.write(task.model_dump_json() + "\n")
            f.flush()
            count += 1
    print(f"mined {count} task(s) into {out}")
    return 0 if count else 1


def cmd_list(args: argparse.Namespace) -> int:
    for t in load_tasks(args.tasks):
        title = t.problem_statement.splitlines()[0][:60] if t.problem_statement else ""
        print(f"{t.instance_id:40}  f2p={len(t.fail_to_pass):<3} p2p={len(t.pass_to_pass):<4} {title}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    bad = 0
    for t in load_tasks(args.tasks):
        empty = evaluate(t, "")
        gold = evaluate(t, t.gold_patch)
        ok = gold.resolved and not empty.resolved
        bad += not ok
        print(
            f"{'ok  ' if ok else 'FAIL'} {t.instance_id}  gold={gold.resolved} empty_patch={empty.resolved}"
        )
    return 1 if bad else 0


def cmd_eval(args: argparse.Namespace) -> int:
    tasks = {t.instance_id: t for t in load_tasks(args.tasks)}
    if args.id not in tasks:
        print(f"no task {args.id}. Try: repo2env list {args.tasks}", file=sys.stderr)
        return 2
    patch = sys.stdin.read() if args.patch == "-" else Path(args.patch).read_text()
    result = evaluate(tasks[args.id], patch, trajectory_path=args.record)
    print(result.model_dump_json(indent=2, exclude={"report": {"output_tail"}}))
    return 0 if result.resolved else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repo2env", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="build a Docker env image for a repo")
    p.add_argument("repo", help="git URL or local path")
    p.add_argument("--ref", default="HEAD")
    p.add_argument("--python", default="3.12", help="Python version for the base image")
    p.add_argument("-o", "--output", help="where to write the env spec JSON")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("mine", help="mine tasks from the repo's git history")
    p.add_argument("env", help="env spec JSON from `repo2env build`")
    p.add_argument("--max-commits", type=int, default=200, help="how far back to look")
    p.add_argument("--limit", type=int, default=10, help="stop after this many tasks")
    p.add_argument("-o", "--output", help="tasks JSONL file")
    p.set_defaults(fn=cmd_mine)

    p = sub.add_parser("list", help="show tasks in a JSONL file")
    p.add_argument("tasks")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("validate", help="check gold patches pass and empty patches fail")
    p.add_argument("tasks")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("eval", help="grade a patch against one task")
    p.add_argument("tasks")
    p.add_argument("--id", required=True, help="instance_id of the task")
    p.add_argument("--patch", required=True, help="diff file, or - for stdin")
    p.add_argument("--record", help="save the grading trajectory here")
    p.set_defaults(fn=cmd_eval)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except Repo2EnvError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
