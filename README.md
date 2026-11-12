# gauntlet-repo2env

Point it at a Python repo, or a Node one tested with vitest. It builds a Docker image with the project and its test deps installed, then digs through the git history for real coding tasks you can hand to an AI agent and grade automatically.

It's the same idea behind SWE-bench, packaged so you can run it on any repo you like in a couple of minutes.

```bash
repo2env build https://github.com/pallets/itsdangerous
repo2env mine pallets__itsdangerous.env.json --limit 4
repo2env validate pallets__itsdangerous.tasks.jsonl
```

That's a real run. The build took 34 seconds (181 tests passing at HEAD), mining took 43 seconds, and it found these:

```
pallets__itsdangerous-7f4dcf83a07b   f2p=41  p2p=0    access sha1 lazily
pallets__itsdangerous-526b1ea02934   f2p=1   p2p=82   date_signed is datetime not int
pallets__itsdangerous-53f34c9a8fed   f2p=45  p2p=46   deprecate jws
pallets__itsdangerous-c30678d19e37   f2p=1   p2p=81   don't allow timestamps from the future
```

`validate` then confirmed each one: the original fix passes, and an empty patch doesn't.

## How a commit becomes a task

1. Walk history newest first and keep commits that touch both source files and test modules.
2. Check out the commit's parent, apply only the test changes, run those tests.
3. Apply the source changes too, and run them again.
4. Tests that flip from failing to passing become `fail_to_pass`. Tests that pass both times become `pass_to_pass`, which catch an agent that fixes one thing by breaking another.
5. No flips means no task, so refactors and dependency bumps drop out on their own.

A patch **resolves** a task when every `fail_to_pass` and `pass_to_pass` test passes after it's applied. The tests are hidden from the agent and get applied on top of its patch at grading time, so it can't edit its way to green.

## Hand a task to an agent

```python
from gauntlet_repo2env import load_tasks, open_task, extract_patch, evaluate

task = load_tasks("pallets__itsdangerous.tasks.jsonl")[3]
print(task.problem_statement)  # "don't allow timestamps from the future"

with open_task(task, trajectory_path="attempt.jsonl") as sb:
    # your agent goes here. It gets a gauntlet-sandbox with the repo at the
    # starting commit, no network, and none of the hidden tests.
    sb.exec("grep -n 'def unsign' src/itsdangerous/timed.py")
    ...
    patch = extract_patch(sb, task)

result = evaluate(task, patch)
print(result.resolved, result.fail_to_pass)
```

Every action the agent takes lands in `attempt.jsonl`, in the [gauntlet-sandbox](https://github.com/abhishekcpp007/gauntlet-sandbox) trajectory format.

## CLI

| Command | What it does |
|---|---|
| `repo2env build URL [--ref REF] [--python 3.12]` | Clone, install, run the suite once, save `<slug>.env.json` |
| `repo2env mine ENV.json [--max-commits 200] [--limit 10]` | Write tasks to `<slug>.tasks.jsonl` as they're found |
| `repo2env list TASKS.jsonl` | One line per task |
| `repo2env validate TASKS.jsonl` | Gold patch must pass, empty patch must fail |
| `repo2env eval TASKS.jsonl --id ID --patch fix.diff` | Grade one patch. Use `--patch -` for stdin |

## What it handles

- `pyproject.toml`, `setup.py`, `setup.cfg` and `requirements*.txt`, in any mix
- Test extras (`.[test]`, `.[tests]`, `.[dev]`) and PEP 735 dependency groups
- Projects that read their version from git tags (setuptools-scm, hatch-vcs), which normally break when built from `git archive`
- Local paths as well as URLs, which is handy for private repos
- Patches that try to game the grader. Before grading, any part of a candidate patch that touches tests, a `conftest.py`, `pytest.ini` or `tox.ini` gets dropped and listed in `ignored_files`. Without this, a 9-line conftest hook that rewrites every report to "passed" scored a resolve. [gauntlet-cheatcatcher](https://github.com/abhishekcpp007/gauntlet-cheatcatcher) flags the subtler stuff.

The build container gets network access so pip can do its thing. Task and grading containers don't.

## Limits worth knowing

- Python with pytest, and Node with vitest. A Node env keeps `node_modules` one folder above the repo, because a task sandbox empties the repo folder before it unpacks a commit, and Node finds packages in any parent folder anyway.
- Node repos that are workspaces, locked with bun, or tested with jest get a clear reason instead of a broken env. So do Go and Rust repos. Mining tasks from git history (`repo2env mine`) is still Python only. Node tasks are built from PRs by gauntlet-harness.
- The env image is built once, at the commit you pick. Tasks from much older history can fail to install or import if dependencies moved a lot. Rebuild with `--ref` closer to that era if you need them.
- The commit message stands in for the problem statement. Pulling the linked GitHub issue text is on the list.

## Part of Gauntlet

This is piece two of **Gauntlet**, an open source proving ground for AI coding agents. [gauntlet-sandbox](https://github.com/abhishekcpp007/gauntlet-sandbox) runs the actions, this turns repos into tasks, and the pieces still to come replay runs, catch agents that cheat, and put it all on a live leaderboard.

## Develop

```bash
uv sync
uv run pytest   # the end-to-end tests need Docker and network, they skip without Docker
```

MIT licensed.
