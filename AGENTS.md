# AGENTS.md — working agreement for this repo

Applies to every contributor here, human or agent: user instructions
(always first) override this file, and this file overrides tool defaults.

## Stack

- Python 3.12 (`requires-python = ">=3.12"`), Linux only (hidraw ioctls).
- Runtime deps: `pillow`, `pyqt6`; optional extra `fallback-tray` (`pystray`).
  `gi` (PyGObject) is an optional system dependency, never a pip one.
- Dev tools live in the `dev` dependency group: `ruff`, `ty`.

## Setup and gates

```bash
uv sync --group dev --extra fallback-tray
uv run python -m unittest discover -s tests   # 76 tests, must stay green
uv run --group dev ruff check .               # must pass, no warnings
uv run --group dev ruff format --check .      # must pass, no diff
uv run --group dev ty check                   # must pass, no errors
```

All four gates must pass before any change is called done. `--extra
fallback-tray` matters: `pystray` is an optional dependency, and `ty`
cannot resolve its imports without it installed. `gi` is intentionally
*not* installed — it is a system package, and its single import site
(`main._load_glib`) carries a targeted `ty: ignore` because every caller
already catches `ImportError`.

## Conventions

- Tests: `unittest` + `unittest.mock`, `tests/test_main.py`. Existing tests
  are extended, not rewritten. New behavior needs a failing test first;
  watch it fail for the right reason before implementing.
- Comments explain *why*, never restate the code.
- `StrixDevice` methods return `bool` for success and leave
  `last_status` / `last_error` semantics untouched.
- No new third-party dependencies without asking.
- Do not reformat or "clean up" code outside your change.

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
  `call-non-callable` for `tests/**` only: the tray tests drive PyQt6 and
  pystray through hand-built fakes assigned where the real classes go,
  and that dynamic trickery is the point of the harness. Production code
  must check clean with no relaxations.
- `docs/` is excluded from Ruff: historical session plans, not shipped code.
