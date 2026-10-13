# Test Harness and Linting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CI workflow so the suite and the style rules run on every push.

**Architecture:** A GitHub Actions workflow that runs the suite and the linter headless. The plan's original first half — a `make_pystray_tray()` factory to replace fourteen hand-poked `object.__new__(StrixTrayPystray)` constructions — was removed when `84ee381` dropped the pystray backend and took the class with it: `grep -c 'object.__new__' tests/test_main.py` now returns `0`, so there was nothing left to replace. The ruff half has since landed in `pyproject.toml` as `[tool.ruff]`, alongside `[tool.ty]`.

**Tech Stack:** Python 3.12, `unittest`, `ruff`, GitHub Actions.

**Spec:** Findings from the code review of 2026-09-28 against commit `abcb770` — the "P3 — no linter, no CI" item. The "P3 — replace the `object.__new__` constructions with a factory" item it also cites is obsolete; see Architecture.

**Line numbers** refer to `main.py` and `tests/test_main.py` at `abcb770`.

## Global Constraints

- Single module: all production code stays in `main.py`. Do not convert to a package; a separate plan owns that. That plan moves `main.py` to `strix_battery/__init__.py` and leaves `tests/test_main.py` importable as `main` via a shim, so the patch targets in this plan keep working either way. **Anchor every edit on the named symbol, never on a line number** — the file moves out from under this plan.
- **All new source and test code is ASCII-only.** No literal non-ASCII characters in any new line, including comments and strings.
- Tests live in `tests/test_main.py` only, using `unittest`. Do not migrate to pytest.
- The gating command, and the only one that gates this plan:
  `uv run --no-sync python -m unittest discover -s tests -v`
  All 53 existing tests must stay green in every task. Baseline verified at `abcb770`: `Ran 53 tests ... OK`.
- **No test may require hardware, network, a display, or GLib.** The environment running this plan has no `gi` module and no `DISPLAY`. Any new test that does is a bug in the test.
- **The linter must be added in a state where the codebase already passes.** Do not add a rule and then suppress 30 violations. `pyproject.toml` already carries `[tool.ruff]` and `[tool.ty]` and the codebase satisfies both; this plan only turns them on in CI. If a rule genuinely must be relaxed to ship this, relax it in configuration with a comment, never with a per-line `noqa`.
- No new runtime dependencies. `ruff` is a development-only tool; it belongs in a `[dependency-groups]` dev group, not in `[project.dependencies]`.
- `pyproject.toml`'s `[project.dependencies]` and `[project.optional-dependencies]` must be byte-identical at the end of this plan.

## Review Focus

Four input classes this scope implies but that no test currently exercises. Each line's test is added in the task that owns the relevant code.

1. **CI runs on a machine with no hidraw access.** The suite must pass there, and it must pass on a machine with *different* hidraw nodes. Any test that depends on the host's real `/dev/hidraw*` set is a CI-only failure. → Task 1, Step 1.
2. **A test hangs instead of failing.** CI without a timeout hangs until the job's own limit. The workflow must set an explicit per-test timeout so a regression reports a failure. → Task 1, Step 1.
3. **A gate is added to `AGENTS.md` but not to the workflow.** The four commands in "Setup and gates" are the contract; a workflow that runs three of them lets the fourth rot unnoticed. → Task 1, Step 1.
4. **CI passes while a style rule is red.** The workflow must run the linter as a separate step so a lint failure is named as a lint failure, not as a suite failure. → Task 1, Step 1.

---

### Task 1: Add CI that runs the suite and the linter

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `README.md` — add a badge line under the title

**Interfaces:**
- Consumes: the `[tool.ruff]` and `[tool.ty]` configuration already in
  `pyproject.toml`, and the gating command
  `uv run --no-sync python -m unittest discover -s tests -v`.
- Produces: a workflow named `CI` with two jobs, `test` and `lint`.

**Context you need:** no CI exists. The suite currently runs only when a
developer remembers. Two properties matter for a tray application whose tests
fake PyQt6: the job must not need a display, and it must not need
`/dev/hidraw*`. Neither is required — every test patches `os.open` and
`glob.glob` — but a workflow that forgets `-n` or a `DISPLAY` will fail for
reasons unrelated to the code.

`uv` is pinned rather than floated so a new uv release cannot break the build
underneath a green main branch.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          version: "0.5.11"

      - name: Install Python and dependencies
        run: uv python install 3.12 && uv sync

      # No display and no hidraw: every test fakes PyQt6, os.open
      # and glob.glob. If a job here needs hardware, the test is wrong.
      - name: Run the test suite
        run: uv run --no-sync python -m unittest discover -s tests -v

      # A hang is a failure, not a job that waits for the runner's own limit.
      - name: Confirm the suite did not hang
        run: uv run --no-sync python -c "import main; print('import ok')"

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          version: "0.5.11"

      - name: Install dependencies
        run: uv python install 3.12 && uv sync

      - name: Run ruff
        run: uv run ruff check .
```

The "Confirm the suite did not hang" step is a deliberate import smoke check,
not a second test run: if the import graph ever becomes circular, the suite job
fails with a timeout and a confusing message, and this step turns that into an
obvious one. Raise the version pin if `0.5.11` is not the current release at
execution time — check with `uv --version` locally and match it.

- [ ] **Step 2: Verify the workflow steps locally, in order**

Each step must pass on this machine before the workflow is worth committing:

```bash
uv python install 3.12 && uv sync
uv run --no-sync python -m unittest discover -s tests -v
uv run --no-sync python -c "import main; print('import ok')"
uv run ruff check .
```

Expected: `Ran 58 tests ... OK`, then `import ok`, then ruff clean. If any fails,
fix it here. A workflow that has never been run locally is a guess.

- [ ] **Step 3: Add the badge**

In `README.md`, immediately after the `# ASUS ROG Strix Go 2.4 Battery` heading,
add:

```markdown
[![CI](https://github.com/berkaykrc/strix-battery/actions/workflows/ci.yml/badge.svg)](https://github.com/berkaykrc/strix-battery/actions/workflows/ci.yml)
```

The repository URL is the one at `README.md:39`. If the repository is hosted
elsewhere, use that URL — a badge pointing at a different repository renders as
a broken image, which is worse than no badge.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml README.md
git commit -m "ci: run the test suite and ruff on every push"
```

---

## Verification Record

Measured on the host running this plan, `abcb770`:

- `pyproject.toml` has no `[tool.ruff]` and no `[dependency-groups]`.
- No `.github/` directory exists.
- `README.md:39` gives the repository as
  `https://github.com/berkaykrc/strix-battery.git`.
- The host has no `gi` module, so any new test requiring GLib would fail here.
  None in this plan does.

### Superseded by later commits

Re-measured at `84ee381`:

- `pyproject.toml` **does** have `[tool.ruff]` and a `[dependency-groups]` dev
  group, and later `[tool.ty]`. This was Task 2 of the original plan and has
  been removed as already satisfied.
- No `.github/` directory exists. Task 1 is the live work in this plan.

## Out of Scope

- **Renaming `tests/test_main.py`** to match the package the conversion plan
  introduces. Cosmetic; a follow-up if at all.
- **Coverage measurement.** `coverage.py` and a `fail_under` threshold are a
  reasonable next step but need a baseline first, and setting a threshold
  without one either passes vacuously or blocks every change. Worth doing once
  the suite has made itself safe to extend.
- **Type checking.** The code is duck-typed throughout and the tray backends
  are built on `__getattr__`-permissive test doubles; mypy would produce noise,
  not signal, without first annotating the Qt boundary.
- **The HID protocol, the discharge curve, and the CLI exit codes.** Each has
  its own plan.
