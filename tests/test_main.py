import contextlib
import io
import itertools
import json
import pathlib
import struct
import subprocess
import sys
import tempfile
import tomllib
import unittest
from types import ModuleType
from typing import ClassVar
from unittest.mock import patch

import main
import strix_battery
from main import (
    ERROR_OFFLINE,
    ERROR_PERMISSION,
    HEADSET_ICON,
    HIDIOCGFEATURE_64,
    HIDIOCGRAWINFO,
    MAX_SLEEP_MINUTES,
    NOTIFY_TIMEOUT_SECONDS,
    StrixDevice,
    mv_to_percent,
    offline_message,
    run_pyqt_tray,
    start_tray,
    validate_sleep_minutes,
)
from strix_battery import battery, device, icon, tray


class PackagingConfigTests(unittest.TestCase):
    """The P2 fix lives in pyproject.toml; pin it where a regression would show."""

    def setUp(self):
        self.config = tomllib.loads(
            (pathlib.Path(main.__file__).parent / "pyproject.toml").read_text()
        )

    def test_the_console_script_points_at_the_package_entry_point(self):
        # A typo here installs a wheel that fails on first run, and the
        # entry-point test cannot see it: it imports the module, not this
        # string.
        self.assertEqual(
            self.config["project"]["scripts"]["strix-battery"],
            "strix_battery.cli:main",
        )

    def test_nothing_is_installed_at_the_top_level(self):
        setuptools_config = self.config["tool"]["setuptools"]
        self.assertNotIn("py-modules", setuptools_config)
        self.assertEqual(setuptools_config["packages"], ["strix_battery"])


class PackageLayoutTests(unittest.TestCase):
    """A generic top-level `main` in site-packages is the reason for this change."""

    def test_the_package_is_importable_and_not_a_top_level_module(self):
        self.assertTrue(strix_battery.__file__.endswith("strix_battery/__init__.py"))
        self.assertNotEqual(
            getattr(strix_battery, "__file__", ""),
            main.__file__,
            "the package and the shim must be two files, not an alias",
        )

    def test_the_shim_still_runs_as_a_script(self):
        # The README documents `uv run python main.py --json`. This runs the
        # real CLI, so the device state is whatever this machine has: assert the
        # plumbing (JSON out, exit code agreeing with the status), not that the
        # headset happens to be absent.
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "main.py", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=pathlib.Path(main.__file__).parent,
        )
        payload = json.loads(result.stdout)  # raises if the shim never ran
        self.assertIn(payload["status"], ("online", "offline"))
        self.assertEqual(
            result.returncode,
            0 if payload["status"] == "online" else 1,
            f"the shim must still reach main(); stderr was: {result.stderr}",
        )

    def test_the_console_script_entry_point_resolves(self):
        import importlib

        module = importlib.import_module("strix_battery.cli")
        self.assertTrue(callable(module.main))

    def test_every_name_the_suite_imports_is_still_exported(self):
        import importlib

        for name in [
            "DEFAULT_DISCHARGE_CURVE",
            "ERROR_OFFLINE",
            "ERROR_PERMISSION",
            "HEADSET_ICON",
            "HIDIOCGFEATURE_64",
            "HIDIOCGRAWINFO",
            "MAX_SLEEP_MINUTES",
            "NOTIFY_TIMEOUT_SECONDS",
            "QUERY_PACKET",
            "SLEEP_ICON",
            "STRIX_PIDS",
            "StrixDevice",
            "load_discharge_curve",
            "main",
            "mv_to_percent",
            "noise_reduction_state_path",
            "offline_message",
            "parse_discharge_curve",
            "pil_to_qpixmap",
            "render_tray_icon",
            "run_pyqt_tray",
            "start_tray",
            "validate_sleep_minutes",
        ]:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(strix_battery, name), f"strix_battery lost {name}"
                )
                self.assertTrue(
                    hasattr(importlib.import_module("strix_battery.cli"), name),
                    f"strix_battery.cli lost {name}",
                )

    def test_the_shim_beats_a_decoy_main_on_the_path(self):
        # What this really proves: the shim that wins is the repo's, and what
        # it re-exports is the package's. (The decoy cannot win by
        # construction — for `-c` the cwd is sys.path[0] and PYTHONPATH comes
        # after it — so the useful assertion is that it never loaded at all.)
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "main.py").write_text("raise SystemExit('decoy won')\n")
            env = {**os.environ, "PYTHONPATH": tmp}
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import main; print(main.__file__); print(main.StrixDevice.__module__)",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
                cwd=pathlib.Path(main.__file__).parent,
            )
        # `SystemExit("...")` prints to stderr, so stdout could never contain it.
        self.assertNotIn("decoy won", result.stderr)
        self.assertNotIn(tmp, result.stdout)
        self.assertIn("strix_battery.device", result.stdout)

    def test_the_shim_exposes_the_private_helper_the_suite_patches(self):
        # `from ... import *` skips underscore names; the suite reaches this one
        # through `main`.
        self.assertIs(
            main._run_qt_app_with_sigint_shutdown,
            strix_battery._run_qt_app_with_sigint_shutdown,
        )


class MvToPercentTests(unittest.TestCase):
    """The percentage curve is the input to every displayed battery number."""

    def test_clamps_to_the_discharge_endpoints(self):
        self.assertEqual(mv_to_percent(3300), 0)
        self.assertEqual(mv_to_percent(4150), 100)
        self.assertEqual(mv_to_percent(3200), 0)
        self.assertEqual(mv_to_percent(4200), 100)

    def test_is_monotonic_across_the_whole_range(self):
        previous = -1
        for mv in range(3200, 4200, 5):
            current = mv_to_percent(mv)
            self.assertGreaterEqual(current, previous, f"regressed at {mv}mV")
            previous = current

    def test_stays_within_bounds_for_garbage_input(self):
        self.assertEqual(mv_to_percent(0), 0)
        self.assertEqual(mv_to_percent(65535), 100)


class PercentageStabilityTests(unittest.TestCase):
    """A noisy voltage must not make the tray icon strobe."""

    def test_the_first_read_is_reported_unchanged(self):
        device = StrixDevice()
        self.assertEqual(device.stabilize_percentage(70), 70)

    def test_jitter_smaller_than_the_deadband_holds_the_reported_value(self):
        device = StrixDevice()
        device.stabilize_percentage(70)
        for noisy in (70, 71, 69, 70, 71, 69):
            self.assertEqual(
                device.stabilize_percentage(noisy),
                70,
                f"{noisy}% is inside the deadband and must not move the display",
            )

    def test_a_move_at_the_deadband_is_reported(self):
        device = StrixDevice()
        device.stabilize_percentage(70)
        self.assertEqual(device.stabilize_percentage(72), 72)

    def test_jitter_around_the_new_value_is_held_at_the_new_value(self):
        device = StrixDevice()
        device.stabilize_percentage(70)
        device.stabilize_percentage(72)
        for noisy in (71, 72, 73, 72):
            self.assertEqual(device.stabilize_percentage(noisy), 72)

    def test_going_offline_clears_the_hold(self):
        device = StrixDevice()
        device.stabilize_percentage(70)
        with patch("strix_battery.device.glob.glob", return_value=[]):
            self.assertIsNone(device.query())
        # 69 would have been held at 70 had the hold survived the disconnect.
        self.assertEqual(device.stabilize_percentage(69), 69)

    def test_low_battery_notifies_exactly_once_across_the_latch_band(self):
        device = StrixDevice()
        with patch.object(device, "notify") as notify:
            for pct in (15, 14, 16, 15, 18, 20):
                device._update_low_battery_notice(pct, False)
        self.assertEqual(notify.call_count, 1)

    def test_low_battery_rearms_above_the_upper_hysteresis_point(self):
        device = StrixDevice()
        with patch.object(device, "notify") as notify:
            for pct in (15, 14, 15):
                device._update_low_battery_notice(pct, False)
            self.assertEqual(notify.call_count, 1)
            device._update_low_battery_notice(21, False)
            for pct in (15, 14):
                device._update_low_battery_notice(pct, False)
        self.assertEqual(notify.call_count, 2)

    def test_low_battery_does_not_notify_while_charging(self):
        device = StrixDevice()
        with patch.object(device, "notify") as notify:
            for pct in (15, 12, 10, 8):
                device._update_low_battery_notice(pct, True)
        notify.assert_not_called()


class NotifyTests(unittest.TestCase):
    """`notify()` runs on the GUI main thread, so it must never block forever."""

    def test_notify_passes_a_timeout_to_the_subprocess(self):
        device = StrixDevice()
        with patch("strix_battery.device.subprocess.run") as run:
            device.notify("ROG Strix Go", "Battery critically low: 12%!")
        self.assertEqual(
            run.call_args.kwargs.get("timeout"),
            NOTIFY_TIMEOUT_SECONDS,
            "notify-send must be bounded so a wedged D-Bus cannot freeze the tray",
        )

    def test_notify_swallows_a_timed_out_notify_send(self):
        device = StrixDevice()
        with patch(
            "strix_battery.device.subprocess.run",
            side_effect=subprocess.TimeoutExpired("notify-send", 5),
        ):
            device.notify("ROG Strix Go", "Battery critically low: 12%!")

    def test_notify_swallows_a_missing_notify_send_binary(self):
        device = StrixDevice()
        with patch(
            "strix_battery.device.subprocess.run",
            side_effect=FileNotFoundError("notify-send"),
        ):
            device.notify("ROG Strix Go", "Battery critically low: 12%!")

    def test_notify_survives_a_failing_notify_send_exit_code(self):
        device = StrixDevice()
        with patch("strix_battery.device.subprocess.run", return_value=None) as run:
            device.notify("ROG Strix Go", "Battery critically low: 12%!")
        self.assertIs(run.call_args.kwargs.get("check"), False)

    def test_notify_swallows_a_bare_oserror(self):
        # EACCES on a non-executable wrapper, EAGAIN when fork hits
        # RLIMIT_NPROC: notify-send can fail with a plain OSError that is
        # neither FileNotFoundError nor a SubprocessError.
        device = StrixDevice()
        with patch(
            "strix_battery.device.subprocess.run",
            side_effect=PermissionError(13, "Permission denied"),
        ):
            device.notify("ROG Strix Go 2.4", "Battery critically low: 12%!")

    def test_a_notification_oserror_does_not_escape_query(self):
        # 3427 mV -> 14%, low enough that query() reaches the notify path.
        def _low_battery_ioctl(fd, request, buf, *args, **kwargs):
            if request == HIDIOCGRAWINFO:
                buf[:8] = ASUS_DEVINFO
            elif request == HIDIOCGFEATURE_64:
                buf[1] = 0x1B
                buf[11] = 0x63
                buf[12] = 0x0D

        device = StrixDevice()
        with (
            patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
            patch("strix_battery.device.os.open", return_value=7),
            patch("strix_battery.device.fcntl.ioctl", side_effect=_low_battery_ioctl),
            patch("strix_battery.device.time.sleep"),
            patch("strix_battery.device.os.close"),
            patch(
                "strix_battery.device.subprocess.run",
                side_effect=PermissionError(13, "Permission denied"),
            ),
        ):
            self.assertIsNotNone(device.query())

    def test_a_notification_oserror_does_not_flip_set_sleep_to_false(self):
        def _ioctl(fd, request, buf, *args, **kwargs):
            if request == HIDIOCGRAWINFO:
                buf[:8] = ASUS_DEVINFO
            elif request == HIDIOCGFEATURE_64:
                buf[1] = 0x1B

        device = StrixDevice()
        with (
            patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
            patch("strix_battery.device.os.open", return_value=7),
            patch("strix_battery.device.fcntl.ioctl", side_effect=_ioctl),
            patch("strix_battery.device.time.sleep"),
            patch("strix_battery.device.os.close"),
            patch(
                "strix_battery.device.subprocess.run",
                side_effect=PermissionError(13, "Permission denied"),
            ),
        ):
            self.assertTrue(device.set_sleep(30))


ASUS_DEVINFO = struct.pack("<Ihh", 5, 0x0B05, 0x18D6)


def deny_first_node(path, *args, **kwargs):
    """`os.open` stand-in: /dev/hidraw0 is denied, every other node opens."""
    if path == "/dev/hidraw0":
        raise PermissionError(13, "Permission denied")
    return 7


def asus_ioctl(devinfo=ASUS_DEVINFO):
    """`fcntl.ioctl` stand-in: report an ASUS device and a valid status reply.

    Writes real bytes so `query()` reaches its success path. A bare MagicMock
    here would leave the read buffer zeroed, which the code correctly reports
    as offline, so the stale-error test would never clear.
    """

    def _ioctl(fd, request, buf, *args, **kwargs):
        if request == HIDIOCGRAWINFO:
            buf[:8] = devinfo
        elif request == HIDIOCGFEATURE_64:
            buf[1] = 0x1B  # status reply marker
            buf[9] = 0x0A  # charging
            buf[11] = 0x40  # voltage LSB; 0x0F40 = 3904 mV
            buf[12] = 0x0F
            buf[21] = 0x00  # sleep seconds
            buf[22] = 0x00

    return _ioctl


class LastErrorTests(unittest.TestCase):
    """A missing udev rule must not be reported as a missing headset."""

    def test_permission_denied_on_a_hidraw_node_is_reported_as_a_permission_error(self):
        device = StrixDevice()
        with (
            patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
            patch(
                "strix_battery.device.os.open",
                side_effect=PermissionError(13, "Permission denied"),
            ),
        ):
            self.assertIsNone(device.query())
        self.assertEqual(device.last_error, ERROR_PERMISSION)

    def test_a_vanished_receiver_is_reported_as_offline(self):
        device = StrixDevice()
        with patch("strix_battery.device.glob.glob", return_value=[]):
            self.assertIsNone(device.query())
        self.assertEqual(device.last_error, ERROR_OFFLINE)

    def test_a_non_permission_open_failure_is_still_reported_as_offline(self):
        device = StrixDevice()
        with (
            patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
            patch(
                "strix_battery.device.os.open",
                side_effect=OSError(19, "No such device"),
            ),
        ):
            self.assertIsNone(device.query())
        self.assertEqual(device.last_error, ERROR_OFFLINE)

    def test_a_successful_query_clears_a_stale_permission_error(self):
        device = StrixDevice()
        with (
            patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
            patch(
                "strix_battery.device.os.open",
                side_effect=PermissionError(13, "Permission denied"),
            ),
        ):
            device.query()
        self.assertEqual(device.last_error, ERROR_PERMISSION)

        with (
            patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
            patch("strix_battery.device.os.open", return_value=7),
            patch("strix_battery.device.fcntl.ioctl", side_effect=asus_ioctl()),
            patch("strix_battery.device.time.sleep"),
            patch("strix_battery.device.os.close"),
        ):
            self.assertIsNotNone(device.query())
        self.assertIsNone(device.last_error)

    def test_a_denied_node_does_not_hide_the_real_receiver(self):
        device = StrixDevice()
        with (
            patch(
                "strix_battery.device.glob.glob",
                return_value=["/dev/hidraw0", "/dev/hidraw1"],
            ),
            patch("strix_battery.device.os.open", side_effect=deny_first_node),
            patch("strix_battery.device.os.close"),
            patch("strix_battery.device.fcntl.ioctl", side_effect=asus_ioctl()),
            patch("strix_battery.device.time.sleep"),
        ):
            _fd, path = device.find_node()
        self.assertEqual(path, "/dev/hidraw1")
        self.assertFalse(device._permission_denied)

    def test_offline_message_names_the_udev_rules_for_a_permission_error(self):
        message = offline_message(ERROR_PERMISSION)
        self.assertIn("permission", message.lower())
        self.assertIn("udev", message.lower())
        self.assertNotEqual(message, offline_message(ERROR_OFFLINE))

    def test_offline_message_stays_generic_when_the_headset_is_simply_absent(self):
        self.assertEqual(
            offline_message(ERROR_OFFLINE), "Headset: Offline / Out of range"
        )

    def test_offline_message_defaults_to_offline_for_an_unknown_error(self):
        self.assertEqual(offline_message(None), "Headset: Offline / Out of range")


class PermissionHintTests(unittest.TestCase):
    """A denied node is a hint, never the verdict: plenty of hosts have one.

    This host has root:root 0660 hidraw nodes for built-in laptop HID, and the
    README udev rule only matches 0b05:18d6/18d7, so those nodes are denied even
    when the rule is installed. An unplugged dongle must therefore still read as
    offline rather than as a missing permission fix.
    """

    def test_the_permission_hint_does_not_replace_the_offline_diagnosis(self):
        message = offline_message(ERROR_PERMISSION).lower()
        self.assertIn("offline", message)
        self.assertIn("permission", message)

    def test_denied_nodes_plus_a_missing_dongle_still_reads_as_offline_first(self):
        device = StrixDevice()
        with (
            patch(
                "strix_battery.device.glob.glob",
                return_value=["/dev/hidraw0", "/dev/hidraw1"],
            ),
            patch(
                "strix_battery.device.os.open",
                side_effect=PermissionError(13, "Permission denied"),
            ),
        ):
            self.assertIsNone(device.query())
        self.assertEqual(device.last_error, ERROR_PERMISSION)
        message = offline_message(device.last_error).lower()
        self.assertIn("offline", message)
        self.assertIn("permission", message)


class _PermissiveType(type):
    """Lets the fake classes answer class-level attribute access.

    `pil_to_qpixmap` does `QImage.Format.Format_RGBA8888`, which is a lookup on
    the class, not an instance, so instance `__getattr__` cannot help.
    """

    def __getattr__(cls, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return FakeQtWidget()


class FakeQtWidget(metaclass=_PermissiveType):
    """Accepts any constructor arguments and tolerates any method call.

    Used with `permissive=True` so `run_pyqt_tray` can be driven all the way
    into `update_state`, which the strict bare-`type()` fakes cannot reach:
    they die on `QAction("...", menu)`.
    """

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return FakeQtWidget()

    def __getattr__(self, name):
        return FakeQtWidget()


class FakeQApplication(FakeQtWidget):
    """Just enough QApplication for the guard and for `update_state` to run."""

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def setQuitOnLastWindowClosed(value):
        pass

    def exec(self):
        return 0


class FakeSignal:
    """Records connections so a test can fire one."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class RecordingAction(FakeQtWidget):
    """Records `setText` so the tray menu item's label can be asserted."""

    instances: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        RecordingAction.instances.append(self)
        # QAction's first positional argument is its label. Record it
        # so a test can find an action by the text it was created
        # with, not only by a later setText call.
        self.text = args[0] if args else None
        self.triggered = FakeSignal()
        self.checkable = False
        self.checked = False

    def setText(self, text):
        self.text = text

    def setCheckable(self, checkable):
        self.checkable = checkable

    def setChecked(self, checked):
        self.checked = checked

    def isChecked(self):
        return self.checked


class RecordingTrayIcon(FakeQtWidget):
    """A tray icon that records the tooltip the user would actually see."""

    tray_available = False
    instances: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        RecordingTrayIcon.instances.append(self)
        self.tooltip = None
        self.shown = False

    @classmethod
    def isSystemTrayAvailable(cls):
        return cls.tray_available

    def setToolTip(self, tooltip):
        self.tooltip = tooltip

    def show(self):
        self.shown = True

    def isVisible(self):
        return self.shown


class _ConnectableSignal:
    """A signal stub that records what the production code connects to it."""

    def __init__(self, owner):
        self._owner = owner

    def connect(self, callback):
        self._owner.timeout_callbacks.append(callback)


class RecordingTimer:
    """Captures the refresh-timer connection so a test can fire the poll."""

    instances: ClassVar[list] = []

    def __init__(self):
        self.timeout_callbacks = []
        self.scheduled = []
        self.started_with = None
        self.timeout = _ConnectableSignal(self)
        RecordingTimer.instances.append(self)

    def start(self, interval):
        self.started_with = interval

    def singleShot(self, delay, callback):
        self.scheduled.append((delay, callback))

    def fire_timeout(self):
        for callback in self.timeout_callbacks:
            callback()


def install_fake_pyqt6(testcase, tray_available, permissive=False, timer=None):
    """Point every `from PyQt6...` import in `main` at in-memory fakes.

    `permissive=False` keeps the strict bare-`type()` stubs, which die the moment
    real widget construction starts. That strictness is what proves the
    tray-availability guard is load-bearing. `permissive=True` swaps in
    `FakeQtWidget` so the code can run past that point.

    `timer` overrides the `QTimer` class so a test can fire the refresh poll;
    the permissive fake swallows every call, which is fine everywhere else.
    """
    RecordingTrayIcon.tray_available = tray_available
    RecordingTrayIcon.instances = []
    RecordingAction.instances = []
    RecordingTimer.instances = []

    # A factory, not a value: each call must hand back a class, because the
    # production code does `QMenu()` / `QAction(...)` / `QTimer()`.
    stub = (lambda: FakeQtWidget) if permissive else (lambda: type("Stub", (), {}))

    qtwidgets = ModuleType("PyQt6.QtWidgets")
    qtwidgets.QApplication = FakeQApplication
    qtwidgets.QMenu = stub()
    qtwidgets.QSystemTrayIcon = RecordingTrayIcon

    qtcore = ModuleType("PyQt6.QtCore")
    qtcore.QTimer = timer if timer is not None else stub()

    qtgui = ModuleType("PyQt6.QtGui")
    qtgui.QAction = RecordingAction if permissive else stub()
    qtgui.QIcon = stub()
    qtgui.QImage = stub()
    qtgui.QPixmap = stub()

    package = ModuleType("PyQt6")
    package.__path__ = []

    patcher = patch.dict(
        "sys.modules",
        {
            "PyQt6": package,
            "PyQt6.QtCore": qtcore,
            "PyQt6.QtGui": qtgui,
            "PyQt6.QtWidgets": qtwidgets,
        },
    )
    patcher.start()
    testcase.addCleanup(patcher.stop)


class QtTrayAvailabilityTests(unittest.TestCase):
    """A desktop with no tray must get a clear message, not a silent hang."""

    def test_run_pyqt_tray_exits_when_no_system_tray_host_exists(self):
        install_fake_pyqt6(self, tray_available=False)
        printed = []
        with (
            patch("builtins.print", side_effect=printed.append),
            self.assertRaises(SystemExit) as raised,
        ):
            run_pyqt_tray(StrixDevice())
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("no system tray", " ".join(printed).lower())

    def test_the_guard_exits_rather_than_raising_import_error(self):
        # A BaseException must escape, not be swallowed by an import fallback.
        install_fake_pyqt6(self, tray_available=False)
        with patch("builtins.print"), self.assertRaises(SystemExit):
            run_pyqt_tray(StrixDevice())

    def test_start_tray_reports_a_tray_less_desktop_instead_of_hanging(self):
        install_fake_pyqt6(self, tray_available=False)
        printed = []
        with (
            patch("builtins.print", side_effect=printed.append),
            self.assertRaises(SystemExit) as raised,
        ):
            start_tray(StrixDevice())
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("no system tray", " ".join(printed).lower())

    def test_start_tray_exits_when_pyqt6_is_unusable(self):
        printed = []
        with (
            patch(
                "strix_battery.tray.run_pyqt_tray",
                side_effect=ImportError("No module named 'PyQt6'"),
            ),
            patch("builtins.print", side_effect=printed.append),
            self.assertRaises(SystemExit) as raised,
        ):
            start_tray(StrixDevice())
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("PyQt6", " ".join(printed))


class OfflineMessageWiringTests(unittest.TestCase):
    """`offline_message` must reach the user through every tray surface.

    Reverting any of these call sites to a hardcoded string must fail the
    suite; without these tests the whole user-visible outcome of the permission
    fix is unverified.
    """

    def test_qt_tray_tooltip_carries_the_message(self):
        install_fake_pyqt6(self, tray_available=True, permissive=True)
        device = StrixDevice()
        device.last_error = ERROR_PERMISSION
        with (
            patch.object(device, "query", return_value=None),
            self.assertRaises(SystemExit),
        ):
            run_pyqt_tray(device)
        tray = RecordingTrayIcon.instances[-1]
        self.assertEqual(tray.tooltip, offline_message(ERROR_PERMISSION))
        self.assertTrue(tray.shown)

    def test_qt_tray_tooltip_falls_back_to_plain_offline(self):
        install_fake_pyqt6(self, tray_available=True, permissive=True)
        device = StrixDevice()
        device.last_error = ERROR_OFFLINE
        with (
            patch.object(device, "query", return_value=None),
            self.assertRaises(SystemExit),
        ):
            run_pyqt_tray(device)
        tray = RecordingTrayIcon.instances[-1]
        self.assertEqual(tray.tooltip, offline_message(ERROR_OFFLINE))

    def test_qt_menu_item_carries_the_message(self):
        install_fake_pyqt6(self, tray_available=True, permissive=True)
        device = StrixDevice()
        device.last_error = ERROR_PERMISSION
        with (
            patch.object(device, "query", return_value=None),
            self.assertRaises(SystemExit),
        ):
            run_pyqt_tray(device)
        # Assert by membership, not by creation order, so reordering the action
        # construction does not break this.
        texts = [a.text for a in RecordingAction.instances if a.text is not None]
        self.assertIn(f"{HEADSET_ICON} {offline_message(ERROR_PERMISSION)}", texts)


class DischargeCurveTests(unittest.TestCase):
    """A piecewise curve replaces the linear map; these pin its guarantees."""

    LINEAR = ((3300, 0), (4150, 100))

    def test_the_discharge_curve_lives_in_the_battery_module(self):
        self.assertIs(main.mv_to_percent, battery.mv_to_percent)
        self.assertIs(main.load_discharge_curve, battery.load_discharge_curve)
        self.assertIs(main.parse_discharge_curve, battery.parse_discharge_curve)

    def test_the_shipped_table_reproduces_the_linear_map_exactly(self):
        # Task 1 must be behaviour-preserving. If this fails, the default table
        # has drifted and every reported percentage is now unverified.
        curve = main.DEFAULT_DISCHARGE_CURVE
        self.assertEqual(curve, self.LINEAR)
        # Clear the environment so this pins the *shipped* table rather than
        # whatever STRIX_BATTERY_CURVE the developer happens to have exported.
        with patch.dict("os.environ"):
            for mv in range(3200, 4200, 7):
                expected = (
                    0
                    if mv <= 3300
                    else 100
                    if mv >= 4150
                    else int((mv - 3300) / (4150 - 3300) * 100)
                )
                self.assertEqual(main.mv_to_percent(mv), expected, f"{mv}mV moved")

    def test_percentages_never_fall_as_voltage_falls(self):
        previous = -1
        with patch.dict("os.environ"):
            for mv in range(3200, 4200, 5):
                current = main.mv_to_percent(mv)
                self.assertGreaterEqual(current, previous, f"regressed at {mv}mV")
                previous = current

    def test_the_curve_is_ordered_by_voltage_and_clamped_at_both_ends(self):
        with patch.dict("os.environ"):
            self.assertEqual(main.mv_to_percent(0), 0)
            self.assertEqual(main.mv_to_percent(65535), 100)

    def test_an_exact_calibration_point_is_hit_exactly(self):
        curve = ((3300, 0), (3600, 25), (4000, 80), (4150, 100))
        for mv, pct in curve:
            self.assertEqual(main.mv_to_percent(mv, curve=curve), pct)

    def test_a_voltage_between_points_interpolates_monotonically(self):
        curve = ((3300, 0), (3600, 25), (4000, 80), (4150, 100))
        values = [main.mv_to_percent(mv, curve=curve) for mv in range(3300, 4151, 25)]
        self.assertEqual(values, sorted(values))
        self.assertLessEqual(min(values), 25)
        self.assertGreaterEqual(max(values), 80)

    def test_a_voltage_between_points_interpolates_rather_than_steps(self):
        # Monotonicity alone cannot tell interpolation from a step function:
        # both are non-decreasing and hit the same endpoints. Assert each
        # segment's midpoint lands halfway, which a step function cannot do.
        curve = ((3300, 0), (3600, 25), (4000, 80), (4150, 100))
        for (low_mv, low_pct), (high_mv, high_pct) in itertools.pairwise(curve):
            midpoint = main.mv_to_percent((low_mv + high_mv) // 2, curve=curve)
            self.assertAlmostEqual(
                midpoint,
                (low_pct + high_pct) / 2,
                delta=2,
                msg=f"{low_mv}-{high_mv}mV stepped instead of interpolating",
            )

    def test_a_voltage_outside_the_table_clamps_rather_than_extrapolating(self):
        curve = ((3300, 0), (3600, 25), (4000, 80), (4150, 100))
        self.assertEqual(main.mv_to_percent(3000, curve=curve), 0)
        self.assertEqual(main.mv_to_percent(4200, curve=curve), 100)
        self.assertEqual(main.mv_to_percent(65535, curve=curve), 100)

    def test_the_env_var_format_round_trips(self):
        spec = "3300:0,3600:25,4000:80,4150:100"
        self.assertEqual(
            main.parse_discharge_curve(spec),
            ((3300, 0), (3600, 25), (4000, 80), (4150, 100)),
        )

    def test_a_malformed_curve_is_rejected(self):
        specs = [
            "",
            "3300",
            "3300:0:5",
            "abc",
            "3300:0,",
            "3600:20,3300:0",  # voltage must ascend
            "3300:80,4150:20",  # percentage must not fall
            "3300:0,4150:101",  # percentage above 100
            "3300:0,4150:-1",  # percentage below 0
        ]
        for spec in specs:
            with self.assertRaises(ValueError, msg=spec):
                main.parse_discharge_curve(spec)

    def test_a_malformed_env_var_falls_back_instead_of_breaking_the_tray(self):
        err = io.StringIO()
        with (
            patch.dict("os.environ", {main.DISCHARGE_CURVE_ENV_VAR: "garbage"}),
            contextlib.redirect_stderr(err),
        ):
            curve = main.load_discharge_curve()
        self.assertEqual(curve, main.DEFAULT_DISCHARGE_CURVE)
        self.assertIn("STRIX_BATTERY_CURVE", err.getvalue())

    def test_a_valid_env_var_is_honoured(self):
        with patch.dict(
            "os.environ",
            {main.DISCHARGE_CURVE_ENV_VAR: "3300:0,3600:25,4150:100"},
        ):
            curve = main.load_discharge_curve()
        self.assertEqual(curve, ((3300, 0), (3600, 25), (4150, 100)))

    def test_the_env_var_actually_reaches_the_percentage(self):
        # load_discharge_curve is not the deliverable on its own; the override
        # is only real if mv_to_percent honours it.
        with patch.dict(
            "os.environ",
            {main.DISCHARGE_CURVE_ENV_VAR: "3300:0,3600:25,4150:100"},
        ):
            # 65 on this curve vs 70 on the linear default, so the assertion
            # fails if the override ever stops being consulted.
            self.assertEqual(main.mv_to_percent(3900), 65)

    def test_a_bad_env_var_warns_once_not_on_every_poll(self):
        # Both tray backends poll every 15s, so a per-call warning becomes
        # thousands of identical lines a day and buries the message.
        err = io.StringIO()
        with (
            patch.dict("os.environ", {main.DISCHARGE_CURVE_ENV_VAR: "not-a-curve"}),
            contextlib.redirect_stderr(err),
        ):
            for _ in range(5):
                main.mv_to_percent(3900)
        self.assertEqual(
            err.getvalue().count("STRIX_BATTERY_CURVE"),
            1,
            "the warning must not repeat on every poll",
        )


class QtHiddenTrayPollTests(unittest.TestCase):
    """A hidden tray icon must stop the poll, or it queries the device forever.

    Desktop environments hide the tray icon when a fullscreen menu opens, so
    without the guard the app keeps hitting the HID device every 15 seconds
    while nothing is on screen.
    """

    def start_tray(self):
        install_fake_pyqt6(
            self, tray_available=True, permissive=True, timer=RecordingTimer
        )
        device = StrixDevice()
        # The mock must outlive this method: firing the timer after the patch
        # context closes would call the real query and touch real hardware.
        patcher = patch.object(device, "query", return_value=None)
        self.query = patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(SystemExit):
            run_pyqt_tray(device)
        self.tray = RecordingTrayIcon.instances[-1]
        self.timer = RecordingTimer.instances[-1]

    def test_the_poll_stops_while_the_icon_is_hidden(self):
        self.start_tray()
        self.tray.shown = False
        self.query.reset_mock()

        self.timer.fire_timeout()

        self.query.assert_not_called()

    def test_the_poll_still_runs_while_the_icon_is_visible(self):
        self.start_tray()
        self.query.reset_mock()

        self.timer.fire_timeout()

        self.query.assert_called_once()

    def test_the_first_refresh_happens_even_though_the_icon_is_not_shown_yet(self):
        # initialize_tray refreshes before tray.show(), so the visibility guard
        # must live on the timer callback and not inside update_state.
        install_fake_pyqt6(
            self, tray_available=True, permissive=True, timer=RecordingTimer
        )
        device = StrixDevice()
        patcher = patch.object(device, "query", return_value=None)
        query = patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(SystemExit):
            run_pyqt_tray(device)
        query.assert_called_once()


class DeviceLayerTests(unittest.TestCase):
    """The HID layer must be reachable only through `device`."""

    def test_the_device_layer_lives_in_the_device_module(self):
        self.assertIs(main.StrixDevice, device.StrixDevice)
        self.assertIs(main.offline_message, device.offline_message)
        self.assertIs(
            main.noise_reduction_state_path, device.noise_reduction_state_path
        )

    def test_a_patched_state_path_is_actually_reached_by_the_constructor(self):
        # The one patch whose failure is invisible: if this patch does not
        # bite, StrixDevice reads the REAL ~/.config path and the suite still
        # goes green - it just writes to the developer's home directory.
        reached = []

        def fake_path():
            reached.append(1)
            raise OSError("no state file for this test")

        with patch(
            "strix_battery.device.noise_reduction_state_path", side_effect=fake_path
        ):
            main.StrixDevice()

        self.assertEqual(len(reached), 1, "the state path was never called")


class FakeDevice:
    """In-memory stand-in for StrixDevice used by the CLI tests."""

    def __init__(self):
        self.last_error = None
        self.status = {
            "percentage": 72,
            "voltage": 3904,
            "charging": True,
            "sleep_min": 30,
            "path": "/dev/hidraw3",
        }
        self.sleep_result = True
        self.sleep_calls = []
        self.query_result = dict(self.status)

    def set_sleep(self, minutes):
        self.sleep_calls.append(minutes)
        return self.sleep_result

    def query(self):
        return self.query_result


def run_cli(argv, device=None):
    """Run main() with a stub device; return (exit_code, stdout, stderr)."""
    device = device or FakeDevice()
    out, err = io.StringIO(), io.StringIO()
    with (
        patch.object(sys, "argv", ["strix-battery", *argv]),
        patch("strix_battery.StrixDevice", return_value=device),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        try:
            main.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class CliSleepTests(unittest.TestCase):
    """CLI sleep distinction between bad args and absent device."""

    def run_main(self, argv, device=None):
        return run_cli(argv, device)

    def test_a_negative_duration_is_rejected_as_a_usage_error(self):
        code, _, err = self.run_main(["--set-sleep", "-1"])
        self.assertEqual(code, 2, "argparse usage errors exit 2")
        self.assertIn("must be between", err)

    def test_a_duration_above_the_cap_is_rejected_as_a_usage_error(self):
        code, _, err = self.run_main(["--set-sleep", str(MAX_SLEEP_MINUTES + 1)])
        self.assertEqual(code, 2)
        self.assertIn("must be between", err)

    def test_a_valid_duration_with_no_receiver_reports_the_receiver(self):
        device = FakeDevice()
        device.sleep_result = False
        code, out, _ = self.run_main(["--set-sleep", "30"], device)
        self.assertEqual(code, 1)
        self.assertIn("could not be reached", out)

    def test_a_successful_set_sleep_exits_zero(self):
        device = FakeDevice()
        device.sleep_result = True
        code, out, _ = self.run_main(["--set-sleep", "30"], device)
        self.assertEqual(code, 0)
        self.assertIn("Sleep timer set to 30 minutes.", out)

    def test_the_validated_minutes_reach_the_device(self):
        device = FakeDevice()
        self.run_main(["--set-sleep", "45"], device)
        self.assertEqual(device.sleep_calls, [45])


class ExitCodeContractTests(unittest.TestCase):
    """A panel script must not see two different verdicts for one headset."""

    def run_main(self, argv, query_result):
        device = FakeDevice()
        device.query_result = query_result
        code, out, _ = run_cli(argv, device)
        return code, out

    ONLINE: ClassVar[dict] = {
        "percentage": 72,
        "voltage": 3904,
        "charging": True,
        "sleep_min": 30,
        "path": "/dev/hidraw3",
    }

    def test_both_modes_report_offline_with_the_same_exit_code(self):
        json_code, _ = self.run_main(["--json"], None)
        text_code, _ = self.run_main([], None)
        self.assertEqual(
            json_code,
            text_code,
            "a Waybar script keying on the exit code silently breaks when only"
            " one of these modes changes",
        )
        self.assertEqual(json_code, main.EXIT_OFFLINE)

    def test_both_modes_report_online_with_exit_zero(self):
        json_code, _ = self.run_main(["--json"], self.ONLINE)
        text_code, _ = self.run_main([], self.ONLINE)
        self.assertEqual(json_code, 0)
        self.assertEqual(text_code, 0)

    def test_json_offline_output_is_unchanged(self):
        _, out = self.run_main(["--json"], None)
        self.assertEqual(
            out,
            json.dumps({"status": "offline", "text": "Headset offline"}) + "\n",
        )

    def test_json_online_output_is_unchanged(self):
        # Readable form of the payload; test_json_output_is_byte_exact is the
        # guard, so this one is documentation, not coverage.
        _, out = self.run_main(["--json"], self.ONLINE)
        self.assertEqual(json.loads(out)["percentage"], 72)
        self.assertEqual(json.loads(out)["text"], "%72 (Charging)")
        self.assertEqual(json.loads(out)["sleep_min"], 30)

    def test_text_online_output_is_unchanged(self):
        # Readable form of the report; test_the_text_report_is_byte_exact is
        # the guard, so this one is documentation, not coverage.
        _, out = self.run_main([], self.ONLINE)
        self.assertIn("--- ROG Strix Go 2.4 Status ---", out)
        self.assertIn("Battery: 72% (3904 mV)", out)
        self.assertIn("Power status: Charging", out)
        self.assertIn("Sleep timer: 30 minutes", out)
        self.assertIn("Device node: /dev/hidraw3", out)

    def test_json_output_is_byte_exact(self):
        # assertIn-line checks survive a reordered or reworded report; the
        # plan's constraint is byte-identical stdout, so pin the whole string.
        _, out = self.run_main(["--json"], self.ONLINE)
        self.assertEqual(
            out,
            json.dumps(
                {
                    "status": "online",
                    "percentage": 72,
                    "voltage": 3904,
                    "charging": True,
                    "sleep_min": 30,
                    "text": "%72 (Charging)",
                }
            )
            + "\n",
        )

    def test_the_text_report_is_byte_exact(self):
        # A missing trailing newline or a dropped blank line passes every
        # assertIn above, so this is the only test that guards the promise.
        _, out = self.run_main([], self.ONLINE)
        self.assertEqual(
            out,
            "--- ROG Strix Go 2.4 Status ---\n"
            "Battery: 72% (3904 mV)\n"
            "Power status: Charging\n"
            "Sleep timer: 30 minutes\n"
            "Device node: /dev/hidraw3\n",
        )

    def test_text_offline_output_is_unchanged(self):
        # The other half of the promise: the offline text is what a shell
        # script reads, and nothing above pinned its bytes.
        _, out = self.run_main([], None)
        self.assertEqual(
            out,
            "ROG Strix Go 2.4 receiver or headset is offline/out of range.\n",
        )

    def test_json_output_is_a_single_parseable_line(self):
        _, out = self.run_main(["--json"], self.ONLINE)
        self.assertEqual(len(out.strip().splitlines()), 1)


class QueryPacketTests(unittest.TestCase):
    """The query packet is sent on every poll, forever, so it must not drift."""

    def test_the_query_packet_is_immutable(self):
        self.assertIsInstance(
            main.QUERY_PACKET,
            bytes,
            "a mutable bytearray lets one bad write corrupt every later query",
        )

    def test_the_query_packet_is_the_expected_sixty_four_byte_report(self):
        self.assertEqual(len(main.QUERY_PACKET), 64)
        self.assertEqual(
            main.QUERY_PACKET[:10],
            bytes([0xFF, 0x08, 0x00, 0xFD, 0x04, 0x12, 0xF1, 0x03, 0x52, 0x01]),
        )

    def test_the_named_offsets_match_the_hardcoded_response_layout(self):
        # These are the offsets the decode expressions used before they were
        # named. If a firmware revision moves a field, this test is where a
        # human should be told, not a user staring at "Offline".
        self.assertEqual(main.REPLY_MARKER, 0x1B)
        self.assertEqual(main.OFFSET_CHARGING, 9)
        self.assertEqual(main.CHARGING_FLAG, 0x0A)
        self.assertEqual(main.OFFSET_VOLUME_LSB, 11)
        self.assertEqual(main.OFFSET_VOLUME_MSB, 12)
        self.assertEqual(main.OFFSET_SLEEP_LSB, 21)
        self.assertEqual(main.OFFSET_SLEEP_MSB, 22)


def make_reply(voltage=3904, charging=True, sleep_sec=0, marker=0x1B):
    """Build a 64-byte status reply the way the receiver would send it."""
    buf = bytearray(64)
    buf[1] = marker
    buf[main.OFFSET_CHARGING] = 0x0A if charging else 0x03
    buf[main.OFFSET_VOLUME_LSB] = voltage & 0xFF
    buf[main.OFFSET_VOLUME_MSB] = (voltage >> 8) & 0xFF
    buf[main.OFFSET_SLEEP_LSB] = sleep_sec & 0xFF
    buf[main.OFFSET_SLEEP_MSB] = (sleep_sec >> 8) & 0xFF
    return buf


class DecodeStatusTests(unittest.TestCase):
    """The decode is pure, so every field can be asserted without hardware."""

    def test_decodes_a_little_endian_voltage(self):
        status = main.decode_status(make_reply(voltage=3904), "/dev/hidraw3")
        self.assertEqual(status["voltage"], 3904)

    def test_decodes_a_voltage_whose_high_byte_is_not_zero(self):
        # 0x1000 = 4096mV. A swapped LSB/MSB would read 16, not 4096.
        status = main.decode_status(make_reply(voltage=4096), "/dev/hidraw3")
        self.assertEqual(status["voltage"], 4096)

    def test_decodes_the_charging_flag(self):
        self.assertIs(
            main.decode_status(make_reply(charging=True), "/d")["charging"], True
        )
        self.assertIs(
            main.decode_status(make_reply(charging=False), "/d")["charging"], False
        )

    def test_decodes_sleep_seconds_as_whole_minutes(self):
        status = main.decode_status(make_reply(sleep_sec=1800), "/dev/hidraw3")
        self.assertEqual(status["sleep_min"], 30)

    def test_carries_the_device_path_through(self):
        status = main.decode_status(make_reply(), "/dev/hidraw7")
        self.assertEqual(status["path"], "/dev/hidraw7")

    def test_a_missing_marker_means_offline(self):
        self.assertIsNone(main.decode_status(make_reply(marker=0x00), "/dev/hidraw3"))

    def test_clamps_a_garbage_voltage_to_a_valid_percentage(self):
        # An all-ones voltage field must not produce a nonsense percentage.
        status = main.decode_status(make_reply(voltage=0xFFFF), "/dev/hidraw3")
        self.assertEqual(status["percentage"], 100)
        self.assertEqual(status["percentage"], mv_to_percent(0xFFFF))

    def test_a_maximum_sleep_field_does_not_crash(self):
        status = main.decode_status(make_reply(sleep_sec=0xFFFF), "/dev/hidraw3")
        self.assertEqual(status["sleep_min"], 1092)

    def test_a_truncated_reply_is_offline_not_a_crash(self):
        # `decode_status` is public, so any caller can hand it a buffer shorter
        # than the fields it reads. A short buffer is a truncated read, not a
        # status: report offline rather than raising IndexError from nowhere.
        self.assertIsNone(main.decode_status(make_reply()[:8], "/dev/hidraw3"))

    def test_an_empty_reply_is_offline_not_a_crash(self):
        self.assertIsNone(main.decode_status(bytearray(), "/dev/hidraw3"))


class SleepPacketTests(unittest.TestCase):
    """The 16-bit seconds field wraps silently, so the encoding must be pinned."""

    def test_encodes_minutes_as_little_endian_seconds(self):
        packet = main.build_sleep_packet(30)
        lsb = packet[main.OFFSET_SLEEP_SECONDS_LSB]
        msb = packet[main.OFFSET_SLEEP_SECONDS_MSB]
        self.assertEqual((msb << 8) | lsb, 1800)

    def test_the_maximum_duration_does_not_wrap(self):
        packet = main.build_sleep_packet(MAX_SLEEP_MINUTES)
        lsb = packet[main.OFFSET_SLEEP_SECONDS_LSB]
        msb = packet[main.OFFSET_SLEEP_SECONDS_MSB]
        encoded = (msb << 8) | lsb
        self.assertEqual(encoded, 65520)
        self.assertLess(encoded, 1 << 16, "the seconds field must not overflow")

    def test_zero_never_turns_the_headset_off(self):
        packet = main.build_sleep_packet(0)
        lsb = packet[main.OFFSET_SLEEP_SECONDS_LSB]
        msb = packet[main.OFFSET_SLEEP_SECONDS_MSB]
        self.assertEqual(((msb << 8) | lsb), 0)

    def test_the_packet_is_sixty_four_bytes(self):
        self.assertEqual(len(main.build_sleep_packet(5)), 64)

    def test_the_command_header_is_frozen(self):
        self.assertEqual(
            main.SLEEP_PACKET_COMMAND,
            bytes([0xFF, 0x0A, 0x00, 0xFF, 0x04, 0x12, 0xF1, 0x05, 0x52, 0x0B]),
        )

    def test_the_packet_is_immutable(self):
        self.assertIsInstance(main.build_sleep_packet(5), bytes)

    def test_rejects_a_duration_that_would_overflow_the_field(self):
        with self.assertRaises(ValueError):
            main.build_sleep_packet(MAX_SLEEP_MINUTES + 1)

    def test_a_relaxed_cap_fails_loudly_instead_of_wrapping(self):
        # The 16-bit field is the only reason the cap is 1092. If the cap is
        # ever relaxed, the overflow must surface here as an error rather than
        # wrap into a timer set to a wrong value with no symptom at all.
        with (
            patch("strix_battery.device.MAX_SLEEP_MINUTES", 2000),
            self.assertRaises(RuntimeError),
        ):
            main.build_sleep_packet(1200)

    def sent_sleep_packet(self, minutes):
        """Run `set_sleep` against a recording ioctl and return every buffer
        handed to HIDIOCSFEATURE_64, in order."""
        sent = []

        def _ioctl(fd, request, buf, *args, **kwargs):
            if request == main.HIDIOCGRAWINFO:
                buf[:8] = ASUS_DEVINFO
            elif request == main.HIDIOCSFEATURE_64:
                sent.append(bytes(buf))
            elif request == main.HIDIOCGFEATURE_64:
                buf[1] = 0x1B
                buf[11] = 0x40
                buf[12] = 0x0F

        device = StrixDevice()
        with (
            patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
            patch("strix_battery.device.os.open", return_value=7),
            patch("strix_battery.device.fcntl.ioctl", side_effect=_ioctl),
            patch("strix_battery.device.time.sleep"),
            patch("strix_battery.device.os.close"),
            patch.object(device, "notify"),
        ):
            self.assertTrue(device.set_sleep(minutes))
        return sent

    def test_set_sleep_writes_the_packet_the_encoder_builds(self):
        # Pins the wiring, not just the encoder: a `set_sleep` that stopped
        # calling `build_sleep_packet` would leave this suite green while the
        # headset received garbage. The trailing query packet comes along for
        # the same ride, which is what pins QUERY_PACKET on the wire too.
        for minutes in (0, 5, 30, MAX_SLEEP_MINUTES):
            with self.subTest(minutes=minutes):
                self.assertEqual(
                    self.sent_sleep_packet(minutes),
                    [
                        main.build_sleep_packet(minutes),
                        main.QUERY_PACKET,
                    ],
                )


class StartTrayMessageTests(unittest.TestCase):
    """A broken Qt install and a missing Qt need different advice."""

    def messages_for(self, pyqt6_found):
        printed = []
        found = object() if pyqt6_found else None

        def fake_run_pyqt_tray(device):
            raise ImportError("No module named 'PyQt6.QtWidgets'")

        with (
            patch("builtins.print", side_effect=printed.append),
            patch("strix_battery.tray.importlib.util.find_spec", return_value=found),
            patch("strix_battery.tray.run_pyqt_tray", side_effect=fake_run_pyqt_tray),
            self.assertRaises(SystemExit) as raised,
        ):
            start_tray(StrixDevice())
        self.assertEqual(raised.exception.code, 1)
        return " ".join(printed)

    def test_a_missing_pyqt6_keeps_the_install_instructions(self):
        message = self.messages_for(pyqt6_found=False)
        self.assertIn("is required for tray mode", message)
        self.assertIn("uv add PyQt6", message)
        self.assertNotIn(
            "could not be used",
            message,
            "a missing PyQt6 is fixed by installing it; say so",
        )

    def test_a_broken_pyqt6_is_not_told_to_install_itself(self):
        message = self.messages_for(pyqt6_found=True)
        self.assertIn("installed but could not be used", message)
        self.assertNotIn(
            "is required for tray mode",
            message,
            "telling a user with a broken Qt to install it again changes nothing",
        )

    def test_a_broken_pyqt6_names_the_real_cause(self):
        message = self.messages_for(pyqt6_found=True)
        self.assertIn("No module named 'PyQt6.QtWidgets'", message)

    def test_each_case_prints_exactly_one_message(self):
        for found in (True, False):
            with self.subTest(found=found):
                message = self.messages_for(found)
                self.assertEqual(message.count("Error:"), 1)

    def test_a_pyqt6_that_cannot_be_inspected_is_treated_as_unusable(self):
        # install_fake_pyqt6-style sys.modules entries have `__spec__ is None`,
        # and find_spec raises ValueError on those. This handler's whole job is
        # to explain the failure, so it must not raise instead.
        printed = []
        with (
            patch.dict(sys.modules, {"PyQt6": ModuleType("PyQt6")}),
            patch("strix_battery.tray.run_pyqt_tray", side_effect=ImportError("boom")),
            patch("builtins.print", side_effect=printed.append),
            self.assertRaises(SystemExit) as raised,
        ):
            start_tray(StrixDevice())
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("installed but could not be used", " ".join(printed))


class SleepDurationTests(unittest.TestCase):
    def test_accepts_values_that_fit_the_hid_field(self):
        self.assertEqual(validate_sleep_minutes(0), 0)
        self.assertEqual(validate_sleep_minutes(MAX_SLEEP_MINUTES), MAX_SLEEP_MINUTES)

    def test_rejects_negative_or_overflowing_values(self):
        with self.assertRaises(ValueError):
            validate_sleep_minutes(-1)
        with self.assertRaises(ValueError):
            validate_sleep_minutes(MAX_SLEEP_MINUTES + 1)


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


class TrayLayerTests(unittest.TestCase):
    """The tray presentation layer must be reachable only through `tray`/`icon`."""

    def test_the_tray_lives_in_its_own_modules(self):
        self.assertIs(main.run_pyqt_tray, tray.run_pyqt_tray)
        self.assertIs(main.start_tray, tray.start_tray)
        self.assertIs(main.render_tray_icon, icon.render_tray_icon)
        self.assertIs(main.pil_to_qpixmap, icon.pil_to_qpixmap)

    def test_a_patched_signal_reaches_the_shutdown_helper(self):
        # `patch("strix_battery.tray.signal", fake)` rebinds a name in main's namespace. Once
        # the helper lives in tray.py that patch would apply and stub nothing.
        controller = FakeSignalController(None)
        with patch("strix_battery.tray.signal", controller):
            main._run_qt_app_with_sigint_shutdown(FakeQtApp(), FakeQtTimer(), None)
        self.assertTrue(
            controller.registrations, "the fake signal module was never installed"
        )


class QtSignalShutdownTests(unittest.TestCase):
    def run_app(self, app, timer, signal_module):
        run_app = getattr(main, "_run_qt_app_with_sigint_shutdown", None)
        self.assertIsNotNone(run_app, "Qt SIGINT lifecycle helper is missing")
        with patch("strix_battery.tray.signal", signal_module):
            return run_app(app, timer)

    def run_app_with_setup(self, app, timer, signal_module, before_exec):
        run_app = getattr(main, "_run_qt_app_with_sigint_shutdown", None)
        self.assertIsNotNone(run_app, "Qt SIGINT lifecycle helper is missing")
        with patch("strix_battery.tray.signal", signal_module):
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


class NoiseReductionPacketTests(unittest.TestCase):
    """The mic noise-reduction packet, byte for byte.

    The command bytes come from the frame G-Helper 0.286 sends for this
    headset: `FF 08 00 FF 04 12 F1 03 52 <0x0C off | 0x0D on>`. Byte 9 is the
    only field that changes, so pinning the whole packet is what catches a
    silent regression in the header, the report ID or the sub-command.
    """

    def sent_packet(self, enabled):
        """Run `set_noise_reduction` against a recording ioctl and return the
        bytes handed to HIDIOCSFEATURE_64."""
        sent = []

        def _ioctl(fd, request, buf, *args, **kwargs):
            if request == main.HIDIOCGRAWINFO:
                buf[:8] = ASUS_DEVINFO
            elif request == main.HIDIOCSFEATURE_64:
                sent.append(bytes(buf))
            elif request == main.HIDIOCGFEATURE_64:
                buf[1] = 0x1B
                buf[11] = 0x40
                buf[12] = 0x0F

        device = StrixDevice()
        # A successful send persists the state, so point the write at
        # this test's own directory: the suite must never touch the
        # real file under ~/.config.
        with (
            tempfile.TemporaryDirectory() as d,
            patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
            patch("strix_battery.device.os.open", return_value=7),
            patch("strix_battery.device.fcntl.ioctl", side_effect=_ioctl),
            patch("strix_battery.device.time.sleep"),
            patch("strix_battery.device.os.close"),
            patch.object(device, "notify"),
            patch(
                "strix_battery.device.noise_reduction_state_path",
                return_value=pathlib.Path(d) / "noise-reduction.json",
            ),
        ):
            self.assertTrue(device.set_noise_reduction(enabled))

        self.assertEqual(len(sent), 1)
        return sent[0]

    def test_enabling_sends_the_on_sub_command(self):
        packet = self.sent_packet(True)
        self.assertEqual(
            packet[:10],
            bytes([0xFF, 0x08, 0x00, 0xFF, 0x04, 0x12, 0xF1, 0x03, 0x52, 0x0D]),
        )

    def test_disabling_sends_the_off_sub_command(self):
        packet = self.sent_packet(False)
        self.assertEqual(
            packet[:10],
            bytes([0xFF, 0x08, 0x00, 0xFF, 0x04, 0x12, 0xF1, 0x03, 0x52, 0x0C]),
        )

    def test_the_packet_is_padded_to_the_report_length(self):
        packet = self.sent_packet(True)
        self.assertEqual(len(packet), 64)
        self.assertEqual(set(packet[10:]), {0x00})

    def test_only_byte_nine_differs_between_the_two_states(self):
        on = self.sent_packet(True)
        off = self.sent_packet(False)
        differing = [i for i in range(64) if on[i] != off[i]]
        self.assertEqual(differing, [9])


class NoiseReductionBehaviourTests(unittest.TestCase):
    """What `set_noise_reduction` reports and how it fails."""

    def setUp(self):
        # These tests assert the device's own state, so they must not
        # inherit whatever the real state file under ~/.config holds
        # from an earlier run.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch(
            "strix_battery.device.noise_reduction_state_path",
            return_value=pathlib.Path(tmp.name) / "noise-reduction.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_new_state_is_recorded_on_the_device(self):
        device = StrixDevice()
        with (
            patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
            patch("strix_battery.device.os.open", return_value=7),
            patch("strix_battery.device.fcntl.ioctl", side_effect=asus_ioctl()),
            patch("strix_battery.device.time.sleep"),
            patch("strix_battery.device.os.close"),
            patch.object(device, "notify"),
        ):
            device.set_noise_reduction(True)
            self.assertTrue(device.noise_reduction)

            device.set_noise_reduction(False)
            self.assertFalse(device.noise_reduction)

    def test_a_missing_receiver_fails_without_claiming_a_state_change(self):
        device = StrixDevice()
        with patch("strix_battery.device.glob.glob", return_value=[]):
            self.assertFalse(device.set_noise_reduction(True))
        self.assertFalse(device.noise_reduction)

    def test_a_denied_node_fails_without_claiming_a_state_change(self):
        device = StrixDevice()
        with (
            patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
            patch(
                "strix_battery.device.os.open",
                side_effect=PermissionError(13, "Permission denied"),
            ),
        ):
            self.assertFalse(device.set_noise_reduction(True))
        self.assertFalse(device.noise_reduction)

    def test_an_ioctl_failure_fails_without_claiming_a_state_change(self):
        device = StrixDevice()
        with (
            patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
            patch("strix_battery.device.os.open", return_value=7),
            patch(
                "strix_battery.device.fcntl.ioctl",
                side_effect=OSError(19, "No such device"),
            ),
            patch("strix_battery.device.os.close"),
        ):
            self.assertFalse(device.set_noise_reduction(True))
        self.assertFalse(device.noise_reduction)


class NoiseReductionStateFileTests(unittest.TestCase):
    """The firmware cannot be asked for this setting, so it must be stored.

    A missing, unreadable or hand-mangled file has to degrade to "off" rather
    than take the tray down at startup.
    """

    def write_state(self, tmp, content):
        path = tmp / "noise_reduction.json"
        path.write_text(content)
        return path

    def test_a_saved_true_is_restored(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = self.write_state(tmp, json.dumps({"noise_reduction": True}))
            with patch(
                "strix_battery.device.noise_reduction_state_path", return_value=path
            ):
                device = StrixDevice()
                device.load_noise_reduction_state()
            self.assertTrue(device.noise_reduction)

    def test_a_missing_file_leaves_the_state_off(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            with patch(
                "strix_battery.device.noise_reduction_state_path",
                return_value=tmp / "absent.json",
            ):
                device = StrixDevice()
                device.load_noise_reduction_state()
            self.assertFalse(device.noise_reduction)

    def test_garbage_json_leaves_the_state_off(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = self.write_state(tmp, "{not json at all")
            with patch(
                "strix_battery.device.noise_reduction_state_path", return_value=path
            ):
                device = StrixDevice()
                device.load_noise_reduction_state()
            self.assertFalse(device.noise_reduction)

    def test_a_saved_non_boolean_leaves_the_state_off(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = self.write_state(tmp, json.dumps({"noise_reduction": "yes"}))
            with patch(
                "strix_battery.device.noise_reduction_state_path", return_value=path
            ):
                device = StrixDevice()
                device.load_noise_reduction_state()
            self.assertFalse(device.noise_reduction)

    def test_the_state_is_written_as_a_boolean_after_a_successful_send(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = tmp / "noise_reduction.json"
            device = StrixDevice()
            with (
                patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
                patch("strix_battery.device.os.open", return_value=7),
                patch("strix_battery.device.fcntl.ioctl", side_effect=asus_ioctl()),
                patch("strix_battery.device.time.sleep"),
                patch("strix_battery.device.os.close"),
                patch.object(device, "notify"),
                patch(
                    "strix_battery.device.noise_reduction_state_path", return_value=path
                ),
            ):
                device.set_noise_reduction(True)
            self.assertEqual(json.loads(path.read_text()), {"noise_reduction": True})

    def test_a_failed_send_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = tmp / "noise_reduction.json"
            device = StrixDevice()
            with patch("strix_battery.device.glob.glob", return_value=[]):
                device.set_noise_reduction(True)
            self.assertFalse(path.exists())

    def test_the_save_replaces_the_file_in_one_step(self):
        # A second process reading mid-write must never see torn JSON.
        # os.replace is the atomic step; the plain open("w") truncated in place.
        device = StrixDevice()
        with (
            tempfile.TemporaryDirectory() as d,
            patch("strix_battery.device.os.replace") as replace,
            patch("strix_battery.device.glob.glob", return_value=["/dev/hidraw0"]),
            patch("strix_battery.device.os.open", return_value=7),
            patch("strix_battery.device.fcntl.ioctl", side_effect=asus_ioctl()),
            patch("strix_battery.device.time.sleep"),
            patch("strix_battery.device.os.close"),
            patch.object(device, "notify"),
            patch(
                "strix_battery.device.noise_reduction_state_path",
                return_value=pathlib.Path(d) / "noise-reduction.json",
            ),
        ):
            device.set_noise_reduction(True)
        replace.assert_called_once()


class HomeLessStateTests(unittest.TestCase):
    """A service or cron process has no HOME; construction must still work."""

    def test_the_device_still_constructs_when_home_is_unset(self):
        with patch(
            "strix_battery.device.pathlib.Path.home",
            side_effect=RuntimeError("Could not determine home directory."),
        ):
            device = StrixDevice()
        self.assertFalse(device.noise_reduction)

    def test_saving_without_home_does_not_raise(self):
        device = StrixDevice()
        with patch(
            "strix_battery.device.pathlib.Path.home",
            side_effect=RuntimeError("Could not determine home directory."),
        ):
            device.save_noise_reduction_state()


class NoiseReductionCliTests(unittest.TestCase):
    """`--noise-reduction on|off` must behave like `--set-sleep`."""

    def run_main(self, argv):
        out = []
        with (
            patch.object(sys, "argv", ["strix-battery", *argv]),
            patch("builtins.print", side_effect=out.append),
        ):
            try:
                main.main()
            except SystemExit as exc:
                return exc.code, out
        return None, out

    def test_on_prints_a_confirmation_and_exits_zero(self):
        device = StrixDevice()
        with (
            patch.object(strix_battery, "StrixDevice", return_value=device),
            patch.object(device, "set_noise_reduction", return_value=True) as setter,
        ):
            code, out = self.run_main(["--noise-reduction", "on"])
        setter.assert_called_once_with(True)
        self.assertIn("noise reduction on", out[0].lower())
        self.assertIsNone(code)

    def test_off_turns_it_off(self):
        device = StrixDevice()
        with (
            patch.object(strix_battery, "StrixDevice", return_value=device),
            patch.object(device, "set_noise_reduction", return_value=True) as setter,
        ):
            self.run_main(["--noise-reduction", "off"])
        setter.assert_called_once_with(False)

    def test_a_failed_send_reports_the_receiver_and_exits_one(self):
        device = StrixDevice()
        with (
            patch.object(strix_battery, "StrixDevice", return_value=device),
            patch.object(device, "set_noise_reduction", return_value=False),
        ):
            code, out = self.run_main(["--noise-reduction", "on"])
        self.assertEqual(code, main.EXIT_OFFLINE)
        self.assertIn("could not be reached", out[0])

    def test_an_unknown_word_is_rejected_before_any_send(self):
        device = StrixDevice()
        with (
            patch.object(sys, "argv", ["strix-battery", "--noise-reduction", "maybe"]),
            patch.object(strix_battery, "StrixDevice", return_value=device),
            patch.object(device, "set_noise_reduction") as setter,
            self.assertRaises(SystemExit) as ctx,
        ):
            main.main()
        setter.assert_not_called()
        self.assertNotEqual(ctx.exception.code, 0)


class QtNoiseReductionMenuTests(unittest.TestCase):
    """The PyQt6 tray must show the state and offer the toggle."""

    def setUp(self):
        # The tray reads the stored state at startup; keep that read
        # inside this test's own directory so a state file left by a
        # real toggle cannot change what these tests start from.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch(
            "strix_battery.device.noise_reduction_state_path",
            return_value=pathlib.Path(tmp.name) / "noise-reduction.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def texts(self):
        return [a.text for a in RecordingAction.instances if a.text is not None]

    def test_the_toggle_button_shows_the_current_state(self):
        install_fake_pyqt6(self, tray_available=True, permissive=True)
        device = StrixDevice()
        device.noise_reduction = True
        with (
            patch.object(device, "query", return_value=None),
            self.assertRaises(SystemExit),
        ):
            run_pyqt_tray(device)
        self.assertIn(
            f"{main.NOISE_REDUCTION_ICON} Microphone Noise Reduction: On",
            self.texts(),
        )

    def test_clicking_the_toggle_button_sends_the_opposite_state(self):
        install_fake_pyqt6(self, tray_available=True, permissive=True)
        device = StrixDevice()
        device.noise_reduction = False
        with (
            patch.object(device, "query", return_value=None),
            patch.object(device, "set_noise_reduction", return_value=True) as setter,
        ):
            with self.assertRaises(SystemExit):
                run_pyqt_tray(device)
            # Fire the signal while the device is still patched:
            # outside this block the lambda would reach the real
            # set_noise_reduction instead of the recording mock.
            toggle = next(
                a
                for a in RecordingAction.instances
                if (a.text or "").startswith(f"{main.NOISE_REDUCTION_ICON} Microphone")
            )
            toggle.triggered.emit(False)
        setter.assert_called_once_with(True)


class PublicSurfaceTests(unittest.TestCase):
    """`import main` is the documented entry point; its surface must not shrink."""

    def test_every_documented_name_is_still_reachable_from_main(self):
        for name in [
            "ASUS_VID",
            "BATTERY_DISPLAY_DEADBAND_PERCENT",
            "ERROR_OFFLINE",
            "ERROR_PERMISSION",
            "HEADSET_ICON",
            "HIDIOCGFEATURE_64",
            "HIDIOCGRAWINFO",
            "LOW_BATTERY_NOTIFY_PERCENT",
            "LOW_BATTERY_REARM_PERCENT",
            "MAX_SLEEP_MINUTES",
            "NOISE_REDUCTION_ICON",
            "NOISE_REDUCTION_SUB_COMMAND",
            "NOTIFY_TIMEOUT_SECONDS",
            "QUERY_PACKET",
            "SLEEP_ICON",
            "STRIX_PIDS",
            "StrixDevice",
            "load_discharge_curve",
            "main",
            "mv_to_percent",
            "noise_reduction_state_path",
            "offline_message",
            "parse_discharge_curve",
            "pil_to_qpixmap",
            "render_tray_icon",
            "run_pyqt_tray",
            "start_tray",
            "validate_sleep_minutes",
        ]:
            with self.subTest(name=name):
                self.assertTrue(hasattr(main, name), f"main lost {name}")
