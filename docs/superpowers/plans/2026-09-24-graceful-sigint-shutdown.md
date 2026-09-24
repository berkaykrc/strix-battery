# Graceful Qt Tray Shutdown on SIGINT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the PyQt6 tray application exit cleanly on Ctrl+C without allowing `KeyboardInterrupt` to escape a Qt slot and trigger `qFatal()`/`SIGABRT`.

**Architecture:** Move Qt startup and event-loop ownership into a small helper that temporarily installs a `SIGINT` handler. The handler defers `QApplication.quit()` with a zero-delay `QTimer.singleShot()` callback; after installation, the helper runs the blocking tray setup callback and then enters `QApplication.exec()`, restoring the previous process handler in `finally`. Unit tests use fake Qt app, timer, and signal objects; no GUI or hardware is required.

**Tech Stack:** Python 3.12, PyQt6 6.11, `unittest`, `unittest.mock`, uv

**Spec:** User-approved bounded design from 2026-09-24: handle Ctrl+C by scheduling `app.quit()` through `QTimer.singleShot(0, ...)` rather than letting `KeyboardInterrupt` leave `update_state`.

## Global Constraints

- Keep Python requirement at 3.12+ and add no dependencies.
- Change only the PyQt6 tray event-loop path; Pystray fallback behavior remains unchanged.
- Install the custom `SIGINT` handler before the initial blocking `update_state()` call and before entering `QApplication.exec()`.
- Queue `app.quit`; never call Qt shutdown synchronously from the Python signal handler.
- Restore the exact previous `SIGINT` handler on normal exit and exceptional exit.
- Preserve the integer return value from `QApplication.exec()` through `sys.exit()`.
- Do not create intermediate commits; create one final commit after all verification passes.

## Review Focus

- SIGINT while a Python Qt slot such as `update_state()` is active must schedule shutdown without an exception escaping; Task 1, `test_sigint_handler_defers_app_quit`.
- SIGINT during initial tray setup must be handled after registration but before `app.exec()` runs; Task 1, `test_startup_callback_runs_after_sigint_handler_is_installed`.
- Repeated Ctrl+C input must remain harmless and request the same clean shutdown; Task 1, `test_repeated_sigint_requests_schedule_safe_quits`.
- An exception from the Qt event loop must not leave a temporary process signal handler installed; Task 1, `test_event_loop_error_restores_previous_sigint_handler`.
- Normal tray exit must preserve the event-loop exit code and restore the previous handler; Task 1, `test_normal_exit_returns_event_loop_code_and_restores_handler`.

## File Structure

- `main.py`: owns Qt event-loop startup, temporary signal registration, deferred quit scheduling, and handler restoration.
- `tests/test_main.py`: owns hardware-free lifecycle tests for the new helper.
- `docs/superpowers/plans/2026-09-24-graceful-sigint-shutdown.md`: records this implementation and verification plan.

---

### Task 1: Add Deferred Qt Shutdown for SIGINT

**Files:**
- Modify: `main.py:1-12,262-342`
- Modify: `tests/test_main.py:1-6,352-354`
- Create: `docs/superpowers/plans/2026-09-24-graceful-sigint-shutdown.md`

**Interfaces:**
- Consumes: `signal.getsignal(signal.SIGINT)`, `signal.signal(signal.SIGINT, handler)`, `QTimer.singleShot(0, app.quit)`, and `app.exec()`.
- Produces: `_run_qt_app_with_sigint_shutdown(app, timer, before_exec=None) -> int`; `run_pyqt_tray()` passes its `QApplication`, `QTimer` class, and protected setup callback to this helper and exits with its returned code.

- [ ] **Step 1: Write failing lifecycle tests**

Update imports in `tests/test_main.py`:

```python
import threading
import unittest
from types import ModuleType
from unittest.mock import patch

import main
from main import MAX_SLEEP_MINUTES, StrixTrayPystray, validate_sleep_minutes
```

Add this test class before the existing `if __name__ == "__main__":` block:

```python
class FakeSignalController:
    def __init__(self, previous_handler=None, events=None, sigint=2):
        self.previous_handler = previous_handler
        self.events = events if events is not None else []
        self.SIGINT = sigint
        self.getsignal_calls = []
        self.registrations = []

    def getsignal(self, signum):
        self.getsignal_calls.append(signum)
        return self.previous_handler

    def signal(self, signum, handler):
        action = "install" if not self.registrations else "restore"
        self.events.append(action)
        self.registrations.append((signum, handler))


class FakeQtApp:
    def __init__(self, result=0, error=None, events=None):
        self.result = result
        self.error = error
        self.events = events if events is not None else []
        self.exec_count = 0
        self.quit_count = 0
        self.exec_active = False
        self.quit_exec_active_states = []
        self.on_exec = None
        self.timer = None
        self.drain_timer = False

    def exec(self):
        self.exec_count += 1
        self.events.append("exec")
        self.exec_active = True
        try:
            if self.on_exec is not None:
                self.on_exec()
            if self.drain_timer:
                for _, callback in list(self.timer.scheduled):
                    callback()
            if self.error is not None:
                raise self.error
            return self.result
        finally:
            self.exec_active = False

    def quit(self):
        self.quit_count += 1
        self.quit_exec_active_states.append(self.exec_active)


class FakeQtTimer:
    def __init__(self):
        self.scheduled = []

    def singleShot(self, delay, callback):
        self.scheduled.append((delay, callback))


class QtSignalShutdownTests(unittest.TestCase):
    def run_app(self, app, timer, signal_module):
        run_app = getattr(main, "_run_qt_app_with_sigint_shutdown", None)
        self.assertIsNotNone(run_app, "Qt SIGINT lifecycle helper is missing")
        with patch("main.signal", signal_module):
            return run_app(app, timer)

    def run_app_with_setup(self, app, timer, signal_module, before_exec):
        run_app = getattr(main, "_run_qt_app_with_sigint_shutdown", None)
        self.assertIsNotNone(run_app, "Qt SIGINT lifecycle helper is missing")
        with patch("main.signal", signal_module):
            try:
                return run_app(app, timer, before_exec)
            except TypeError as exc:
                self.fail(f"Qt startup callback is unsupported: {exc}")

    def previous_sigint_handler(self, signum, frame):
        pass

    def assert_previous_handler_restored(self, signal_module, previous_handler):
        self.assertEqual(signal_module.getsignal_calls, [signal_module.SIGINT])
        self.assertEqual(len(signal_module.registrations), 2)
        install_signum, install_handler = signal_module.registrations[0]
        restore_signum, restore_handler = signal_module.registrations[1]
        self.assertEqual(install_signum, signal_module.SIGINT)
        self.assertTrue(callable(install_handler))
        self.assertEqual(restore_signum, signal_module.SIGINT)
        self.assertIs(restore_handler, previous_handler)

    def test_sigint_handler_defers_app_quit(self):
        app = FakeQtApp()
        timer = FakeQtTimer()
        previous_handler = self.previous_sigint_handler
        signal_module = FakeSignalController(previous_handler)

        def deliver_sigint():
            handler = signal_module.registrations[0][1]
            handler(signal_module.SIGINT, None)

        app.on_exec = deliver_sigint
        app.timer = timer
        app.drain_timer = True

        result = self.run_app(app, timer, signal_module)

        self.assertEqual(app.quit_count, 1)
        self.assertEqual(app.quit_exec_active_states, [True])
        self.assertEqual(len(timer.scheduled), 1)
        delay, _ = timer.scheduled[0]
        self.assertEqual(delay, 0)
        self.assertEqual(result, 0)
        self.assert_previous_handler_restored(signal_module, previous_handler)

    def test_startup_callback_runs_after_sigint_handler_is_installed(self):
        order = []
        app = FakeQtApp(events=order)
        timer = FakeQtTimer()
        previous_handler = self.previous_sigint_handler
        signal_module = FakeSignalController(previous_handler, events=order)

        def before_exec():
            order.append("setup")
            handler = signal_module.registrations[0][1]
            handler(signal_module.SIGINT, None)
            order.append("setup_done")

        self.run_app_with_setup(app, timer, signal_module, before_exec)

        self.assertEqual(order, ["install", "setup", "setup_done", "exec", "restore"])
        self.assertEqual([delay for delay, _ in timer.scheduled], [0])
        for _, callback in timer.scheduled:
            callback()
        self.assertEqual(app.quit_count, 1)
        self.assert_previous_handler_restored(signal_module, previous_handler)

    def test_sigint_handler_is_installed_before_event_loop(self):
        order = []
        app = FakeQtApp(events=order)
        timer = FakeQtTimer()
        signal_module = FakeSignalController(events=order)

        self.run_app(app, timer, signal_module)

        self.assertEqual(order, ["install", "exec", "restore"])

    def test_repeated_sigint_requests_schedule_safe_quits(self):
        app = FakeQtApp()
        timer = FakeQtTimer()
        signal_module = FakeSignalController(self.previous_sigint_handler)

        self.run_app(app, timer, signal_module)

        handler = signal_module.registrations[0][1]
        handler(signal_module.SIGINT, None)
        handler(signal_module.SIGINT, None)

        self.assertEqual([delay for delay, _ in timer.scheduled], [0, 0])
        for _, callback in timer.scheduled:
            callback()
        self.assertEqual(app.quit_count, 2)

    def test_event_loop_error_restores_previous_sigint_handler(self):
        previous_handler = self.previous_sigint_handler
        app = FakeQtApp(error=RuntimeError("event loop failed"))
        timer = FakeQtTimer()
        signal_module = FakeSignalController(previous_handler)

        with self.assertRaisesRegex(RuntimeError, "event loop failed"):
            self.run_app(app, timer, signal_module)

        self.assert_previous_handler_restored(signal_module, previous_handler)

    def test_normal_exit_returns_event_loop_code_and_restores_handler(self):
        previous_handler = self.previous_sigint_handler
        app = FakeQtApp(result=17)
        timer = FakeQtTimer()
        signal_module = FakeSignalController(previous_handler)

        result = self.run_app(app, timer, signal_module)

        self.assertEqual(result, 17)
        self.assert_previous_handler_restored(signal_module, previous_handler)
```

- [ ] **Step 2: Run new tests and verify RED**

Run:

```bash
uv run python -m unittest discover -s tests -p 'test_main.py' -k QtSignalShutdownTests -v
```

Expected: import or test failure because `_run_qt_app_with_sigint_shutdown` does not exist yet.

- [ ] **Step 3: Implement the minimal lifecycle helper**

Add `signal` to the standard-library imports in `main.py`:

```python
import os
import signal
import struct
```

Add this helper immediately before `run_pyqt_tray()`:

```python
def _run_qt_app_with_sigint_shutdown(app, timer, before_exec=None):
    previous_sigint_handler = signal.getsignal(signal.SIGINT)

    def request_quit(signum, frame):
        timer.singleShot(0, app.quit)

    signal.signal(signal.SIGINT, request_quit)
    try:
        if before_exec is not None:
            before_exec()
        return app.exec()
    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)
```

Move the potentially blocking initial update and tray setup behind the protected callback:

```python
    refresh_timer = QTimer()

    def initialize_tray():
        update_state()
        tray.show()

        # Her 15 saniyede bir otomatik sorgula
        refresh_timer.timeout.connect(update_state)
        refresh_timer.start(15000)

    sys.exit(_run_qt_app_with_sigint_shutdown(app, QTimer, initialize_tray))
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
uv run python -m unittest discover -s tests -p 'test_main.py' -k QtSignalShutdownTests -v
```

Expected: 6 tests run, all pass.

- [ ] **Step 5: Run the complete unit suite**

Run:

```bash
uv run python -m unittest discover -s tests -v
```

Expected: 20 tests run, all pass.

- [ ] **Step 6: Smoke-test real terminal Ctrl+C handling**

Run from the worktree root:

```bash
env -u VIRTUAL_ENV bash -c '(sleep 1; printf "\003"; sleep 1) | socat - EXEC:"uv run strix-battery --tray",pty,raw,echo=0'
```

Expected: tray process exits without a Python traceback, `Unhandled Python exception`, `qFatal`, or `SIGABRT` message.

Immediately verify no new Python coredump was produced:

```bash
coredumpctl --no-pager list --since "2 minutes ago"
```

Expected: no new `python3.12` coredump from this smoke test.

- [ ] **Step 7: Stage the complete change**

Run:

```bash
git add main.py tests/test_main.py docs/superpowers/plans/2026-09-24-graceful-sigint-shutdown.md
```

Expected: all intended files are staged and no unrelated files are staged.

- [ ] **Step 8: Review the complete staged diff**

Run:

```bash
git diff --cached --check
git diff --cached --stat
git diff --cached -- main.py tests/test_main.py docs/superpowers/plans/2026-09-24-graceful-sigint-shutdown.md
```

Expected: no whitespace errors; staged diff contains only the signal import, protected lifecycle helper, protected startup callback, six tests, and this plan.

- [ ] **Step 9: Create the single final commit**

Run:

```bash
git commit -m "fix: close tray cleanly on Ctrl+C"
```

Expected: one commit on `fix/graceful-sigint-shutdown` containing the plan, regression tests, and implementation.
