# AGENTS.md — working agreement for this repo

Applies to every contributor here, human or agent: user instructions
(always first) override this file, and this file overrides tool defaults.

## Stack

- Python 3.12 (`requires-python = ">=3.12"`), Linux only (hidraw ioctls).
- Runtime deps: `pillow`, `pyqt6`. There are no extras: PyQt6 is the only
  tray backend, so `uv sync` is the whole install.
- Dev tools live in the `dev` dependency group: `ruff`, `ty`.

## Setup and gates

```bash
uv sync --group dev
uv run python -m unittest discover -s tests   # must stay green
uv run --group dev ruff check .               # must pass, no warnings
uv run --group dev ruff format --check .      # must pass, no diff
uv run --group dev ty check                   # must pass, no errors
```

All four gates must pass before any change is called done.

## Conventions

- Tests: `unittest` + `unittest.mock`, `tests/test_main.py`. Existing tests
  are extended, not rewritten. New behavior needs a failing test first;
  watch it fail for the right reason before implementing.
- Comments explain *why*, never restate the code.
- `StrixDevice` methods return `bool` for success and leave
  `last_status` / `last_error` semantics untouched.
- No new third-party dependencies without asking.
- Do not reformat or "clean up" code outside your change.

## Versioning

Semver, pre-1.0 (`0.x.y`): new functionality bumps minor, fixes bump
patch. The version lives only in `pyproject.toml`; refresh `uv.lock` with
`uv lock` in the same change.

The bump rides **with** the change, before push — never as an afterthought
commit on top of pushed history.

Read "the change" as the unit of work, not the commit. A change may span
several commits; one plan is one change even when it lands as two commits.
It still produces exactly one version number, because a release carrying
half of a change is worse than no release at all: a user who installs
0.2.1 for the missing piece gets the other task's fix only if it shipped
too. So bump **once per plan**, not once per commit, and put that single
bump in the change's first commit — the later commits in that change
touch no version line. Two `fix:` commits that deliver one user-visible
outcome get one bump between them, not two.

When a change is already pushed, the rule is not retrofittable: leave the
released version alone and make the next change carry the bump.

## Lint and type decisions (locked)

These were argued once so they don't get re-argued per change:

- Ruff `N` (pep8-naming) is off: the Qt/PyQt API the code and its fakes
  mirror (`setText`, `setCheckable`, `QAction`, ...) is camelCase by
  design. Flagging it would fight the toolkit.
- Ruff `C901` is off: `render_tray_icon` is long because drawing is
  verbose, not because it branches.
- Ruff `E501` is off: the formatter owns code layout and cannot split
  long string literals (packet bytes, help texts). Length stays a review
  judgment, not a gate.
- `ty` overrides relax `invalid-assignment`, `unresolved-attribute` and
  `call-non-callable` for `tests/**` only: the tray tests drive PyQt6
  through hand-built fakes assigned where the real classes go, and that
  dynamic trickery is the point of the harness. Production code must
  check clean with no relaxations.
- `docs/` is excluded from Ruff: historical session plans, not shipped code.
