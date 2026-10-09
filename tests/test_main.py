import contextlib
import io
import itertools
import json
import pathlib
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from types import ModuleType
from typing import ClassVar
from unittest.mock import patch

import main
from main import (
    ERROR_OFFLINE,
    ERROR_PERMISSION,
    HEADSET_ICON,
    HIDIOCGFEATURE_64,
    HIDIOCGRAWINFO,
    MAX_SLEEP_MINUTES,
    NOTIFY_TIMEOUT_SECONDS,
    StrixDevice,
    StrixTrayPystray,
    mv_to_percent,
    offline_message,
    run_pyqt_tray,
    start_tray,
    validate_sleep_minutes,
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
        with patch("main.glob.glob", return_value=[]):
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
        with patch("main.subprocess.run") as run:
            device.notify("ROG Strix Go", "Battery critically low: 12%!")
        self.assertEqual(
            run.call_args.kwargs.get("timeout"),
            NOTIFY_TIMEOUT_SECONDS,
            "notify-send must be bounded so a wedged D-Bus cannot freeze the tray",
        )

    def test_notify_swallows_a_timed_out_notify_send(self):
        device = StrixDevice()
        with patch(
            "main.subprocess.run",
            side_effect=subprocess.TimeoutExpired("notify-send", 5),
        ):
            device.notify("ROG Strix Go", "Battery critically low: 12%!")

    def test_notify_swallows_a_missing_notify_send_binary(self):
        device = StrixDevice()
        with patch("main.subprocess.run", side_effect=FileNotFoundError("notify-send")):
            device.notify("ROG Strix Go", "Battery critically low: 12%!")

    def test_notify_survives_a_failing_notify_send_exit_code(self):
        device = StrixDevice()
        with patch("main.subprocess.run", return_value=None) as run:
            device.notify("ROG Strix Go", "Battery critically low: 12%!")
        self.assertIs(run.call_args.kwargs.get("check"), False)


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
            patch("main.glob.glob", return_value=["/dev/hidraw0"]),
            patch("main.os.open", side_effect=PermissionError(13, "Permission denied")),
        ):
            self.assertIsNone(device.query())
        self.assertEqual(device.last_error, ERROR_PERMISSION)

    def test_a_vanished_receiver_is_reported_as_offline(self):
        device = StrixDevice()
        with patch("main.glob.glob", return_value=[]):
            self.assertIsNone(device.query())
        self.assertEqual(device.last_error, ERROR_OFFLINE)

    def test_a_non_permission_open_failure_is_still_reported_as_offline(self):
        device = StrixDevice()
        with (
            patch("main.glob.glob", return_value=["/dev/hidraw0"]),
            patch("main.os.open", side_effect=OSError(19, "No such device")),
        ):
            self.assertIsNone(device.query())
        self.assertEqual(device.last_error, ERROR_OFFLINE)

    def test_a_successful_query_clears_a_stale_permission_error(self):
        device = StrixDevice()
        with (
            patch("main.glob.glob", return_value=["/dev/hidraw0"]),
            patch("main.os.open", side_effect=PermissionError(13, "Permission denied")),
        ):
            device.query()
        self.assertEqual(device.last_error, ERROR_PERMISSION)

        with (
            patch("main.glob.glob", return_value=["/dev/hidraw0"]),
            patch("main.os.open", return_value=7),
            patch("main.fcntl.ioctl", side_effect=asus_ioctl()),
            patch("main.time.sleep"),
            patch("main.os.close"),
        ):
            self.assertIsNotNone(device.query())
        self.assertIsNone(device.last_error)

    def test_a_denied_node_does_not_hide_the_real_receiver(self):
        device = StrixDevice()
        with (
            patch("main.glob.glob", return_value=["/dev/hidraw0", "/dev/hidraw1"]),
            patch("main.os.open", side_effect=deny_first_node),
            patch("main.os.close"),
            patch("main.fcntl.ioctl", side_effect=asus_ioctl()),
            patch("main.time.sleep"),
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
            patch("main.glob.glob", return_value=["/dev/hidraw0", "/dev/hidraw1"]),
            patch("main.os.open", side_effect=PermissionError(13, "Permission denied")),
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


def install_fake_pyqt6(testcase, tray_available, permissive=False):
    """Point every `from PyQt6...` import in `main` at in-memory fakes.

    `permissive=False` keeps the strict bare-`type()` stubs, which die the moment
    real widget construction starts. That strictness is what proves the
    tray-availability guard is load-bearing. `permissive=True` swaps in
    `FakeQtWidget` so the code can run past that point.
    """
    RecordingTrayIcon.tray_available = tray_available
    RecordingTrayIcon.instances = []
    RecordingAction.instances = []

    # A factory, not a value: each call must hand back a class, because the
    # production code does `QMenu()` / `QAction(...)` / `QTimer()`.
    stub = (lambda: FakeQtWidget) if permissive else (lambda: type("Stub", (), {}))

    qtwidgets = ModuleType("PyQt6.QtWidgets")
    qtwidgets.QApplication = FakeQApplication
    qtwidgets.QMenu = stub()
    qtwidgets.QSystemTrayIcon = RecordingTrayIcon

    qtcore = ModuleType("PyQt6.QtCore")
    qtcore.QTimer = stub()

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
        # start_tray catches ImportError and falls back to pystray, which also
        # needs a tray. SystemExit is a BaseException, so it escapes that catch.
        install_fake_pyqt6(self, tray_available=False)
        with patch("builtins.print"), self.assertRaises(SystemExit):
            run_pyqt_tray(StrixDevice())

    def test_start_tray_does_not_fall_back_to_pystray_on_a_tray_less_desktop(self):
        install_fake_pyqt6(self, tray_available=False)
        with (
            patch("builtins.print"),
            patch("main.importlib.util.find_spec", return_value=object()),
            patch("main.StrixTrayPystray") as tray_class,
            self.assertRaises(SystemExit),
        ):
            start_tray(StrixDevice())
        tray_class.assert_not_called()


class OfflineMessageWiringTests(unittest.TestCase):
    """`offline_message` must reach the user through every tray backend.

    Reverting any of these three call sites to a hardcoded string must fail the
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

    def test_pystray_menu_item_carries_the_message(self):
        tray = object.__new__(StrixTrayPystray)
        device = FakeDevice()
        device.last_error = ERROR_PERMISSION
        tray.device = device
        self.assertEqual(tray.get_status_text(), offline_message(ERROR_PERMISSION))

    def test_pystray_tooltip_carries_the_message(self):
        tray = object.__new__(StrixTrayPystray)
        device = FakeDevice()
        device.last_error = ERROR_PERMISSION
        tray.device = device
        tray.icon = FakePystrayIcon()
        tray._shutting_down = False
        with (
            patch.object(device, "query", return_value=None),
            patch("main.render_tray_icon", return_value=object()),
        ):
            tray.update_tray()
        self.assertEqual(tray.icon.title, offline_message(ERROR_PERMISSION))


class DischargeCurveTests(unittest.TestCase):
    """A piecewise curve replaces the linear map; these pin its guarantees."""

    LINEAR = ((3300, 0), (4150, 100))

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


class SleepDurationTests(unittest.TestCase):
    def test_accepts_values_that_fit_the_hid_field(self):
        self.assertEqual(validate_sleep_minutes(0), 0)
        self.assertEqual(validate_sleep_minutes(MAX_SLEEP_MINUTES), MAX_SLEEP_MINUTES)

    def test_rejects_negative_or_overflowing_values(self):
        with self.assertRaises(ValueError):
            validate_sleep_minutes(-1)
        with self.assertRaises(ValueError):
            validate_sleep_minutes(MAX_SLEEP_MINUTES + 1)


class FakePystrayIcon:
    def __init__(self, device=None):
        self.device = device
        self.visible = False
        self.detached_runs = 0
        self.blocking_runs = 0
        self.stop_count = 0
        self.icon_write_threads = []
        self.title_write_threads = []
        self._icon = None
        self._title = None

    @property
    def icon(self):
        return self._icon

    @icon.setter
    def icon(self, value):
        self.icon_write_threads.append(threading.get_ident())
        self._icon = value

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        self.title_write_threads.append(threading.get_ident())
        self._title = value

    def run_detached(self):
        self.detached_runs += 1

    def run(self):
        self.blocking_runs += 1
        if self.device is not None:
            self.device.query_finished.wait(timeout=1)

    def stop(self):
        self.stop_count += 1


class FakeMainLoop:
    def __init__(self):
        self.run_count = 0
        self.quit_count = 0
        self.run_error = None
        self.dispatch_callback = None

    def run(self):
        self.run_count += 1
        if self.dispatch_callback is not None:
            self.dispatch_callback()
        if self.run_error is not None:
            raise self.run_error

    def quit(self):
        self.quit_count += 1


class FakeGLib:
    def __init__(self, dispatch_timer=False):
        self.main_loop = FakeMainLoop()
        self.dispatch_timer = dispatch_timer
        self.timer_callback = None
        self.timer_interval = None
        self.removed_source_ids = []
        self.source_remove_error = None

    def MainLoop(self):
        return self.main_loop

    def timeout_add_seconds(self, interval, callback):
        self.timer_interval = interval
        self.timer_callback = callback
        if self.dispatch_timer:
            self.main_loop.dispatch_callback = callback
        return 1

    def source_remove(self, source_id):
        self.removed_source_ids.append(source_id)
        if self.source_remove_error is not None:
            raise self.source_remove_error


class FakeDevice:
    def __init__(self):
        self.query_threads = []
        self.query_finished = threading.Event()
        self.query_error = None
        self.after_query = None
        self.status = {
            "percentage": 50,
            "voltage": 4000,
            "charging": True,
            "sleep_min": 0,
        }
        self.last_error = None
        self.last_status = None
        self.noise_reduction = False

    def query(self):
        self.query_threads.append(threading.get_ident())
        self.query_finished.set()
        if self.query_error is not None:
            raise self.query_error
        if self.after_query is not None:
            self.after_query()
        return self.status


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class FakeMenuItem:
    def __init__(self, text, action, **kwargs):
        self.text = text
        self.action = action
        self.submenu = action if isinstance(action, FakeMenu) else None
        self.checked = kwargs.get("checked")
        self.radio = kwargs.get("radio", False)


def find_menu_item(items, text):
    for item in items:
        if not hasattr(item, "text"):
            continue
        if item.text == text:
            return item
        if item.submenu is not None:
            match = find_menu_item(item.submenu.items, text)
            if match is not None:
                return match
    return None


class PystrayPollingTests(unittest.TestCase):
    def test_start_schedules_updates_on_the_main_loop(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        tray.icon.visible = True
        glib = FakeGLib(dispatch_timer=True)
        updates = []
        tray.update_tray = lambda: updates.append("updated")
        tray._main_loop = None
        tray._poll_source_id = None

        with patch("main._load_glib", return_value=glib):
            tray.start()

        self.assertIsNotNone(glib.timer_callback)
        self.assertEqual(glib.timer_interval, 15)
        self.assertEqual(glib.main_loop.run_count, 1)
        self.assertEqual(tray.icon.detached_runs, 1)
        self.assertEqual(tray.icon.blocking_runs, 0)
        self.assertEqual(updates, ["updated"])

    def test_polling_mutates_icon_on_main_loop_thread(self):
        device = FakeDevice()
        icon = FakePystrayIcon(device)
        icon.visible = True
        tray = object.__new__(StrixTrayPystray)
        tray.device = device
        tray.icon = icon
        glib = FakeGLib(dispatch_timer=True)
        main_thread = threading.get_ident()

        with (
            patch("main._load_glib", return_value=glib),
            patch("main.render_tray_icon", return_value=object()),
        ):
            tray.start()

        self.assertEqual(device.query_threads, [main_thread])
        self.assertEqual(icon.icon_write_threads, [main_thread])
        self.assertEqual(icon.title_write_threads, [main_thread])

    def test_poll_clears_source_id_when_refresh_fails(self):
        device = FakeDevice()
        device.query_error = RuntimeError("query failed")
        tray = object.__new__(StrixTrayPystray)
        tray.device = device
        tray.icon = FakePystrayIcon()
        tray.icon.visible = True
        tray._poll_source_id = 1
        tray._shutting_down = False

        with self.assertRaisesRegex(RuntimeError, "query failed"):
            tray.poll()

        self.assertIsNone(tray._poll_source_id)

    def test_start_skips_automatic_polling_without_glib(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        tray._main_loop = None
        tray._poll_source_id = None

        with patch("main._load_glib", side_effect=ImportError), patch("builtins.print"):
            tray.start()

        self.assertEqual(tray.icon.blocking_runs, 1)
        self.assertEqual(tray.icon.detached_runs, 0)

    def test_start_cleans_up_when_main_loop_fails(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        glib = FakeGLib()
        glib.main_loop.run_error = RuntimeError("main loop failed")
        tray._main_loop = None
        tray._poll_source_id = None

        with (
            patch("main._load_glib", return_value=glib),
            self.assertRaisesRegex(RuntimeError, "main loop failed"),
        ):
            tray.start()

        self.assertEqual(glib.removed_source_ids, [1])
        self.assertEqual(tray.icon.stop_count, 1)
        self.assertEqual(glib.main_loop.quit_count, 1)

    def test_quit_stops_polling_icon_and_main_loop(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        glib = FakeGLib()
        tray._glib = glib
        tray._main_loop = glib.main_loop
        tray._poll_source_id = 1
        tray._shutting_down = False

        tray.quit()

        self.assertEqual(glib.removed_source_ids, [1])
        self.assertEqual(tray.icon.stop_count, 1)
        self.assertEqual(glib.main_loop.quit_count, 1)

    def test_quit_continues_cleanup_when_timer_removal_fails(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        glib = FakeGLib()
        glib.source_remove_error = RuntimeError("timer cleanup failed")
        tray._glib = glib
        tray._main_loop = glib.main_loop
        tray._poll_source_id = 1
        tray._shutting_down = False

        with self.assertRaisesRegex(RuntimeError, "timer cleanup failed"):
            tray.quit()

        self.assertEqual(tray.icon.stop_count, 1)
        self.assertEqual(glib.main_loop.quit_count, 1)

    def test_quit_is_idempotent(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        glib = FakeGLib()
        tray._glib = glib
        tray._main_loop = glib.main_loop
        tray._poll_source_id = 1
        tray._shutting_down = False

        tray.quit()
        tray.quit()

        self.assertEqual(glib.removed_source_ids, [1])
        self.assertEqual(tray.icon.stop_count, 1)
        self.assertEqual(glib.main_loop.quit_count, 1)

    def test_poll_stops_when_icon_is_hidden(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        tray._poll_source_id = 1
        tray._shutting_down = False
        updates = []
        tray.update_tray = lambda: updates.append("updated")

        self.assertFalse(tray.poll())
        self.assertIsNone(tray._poll_source_id)
        self.assertEqual(updates, [])

    def test_poll_does_not_update_after_shutdown_starts(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        tray.icon.visible = True
        tray._poll_source_id = 1
        tray._shutting_down = True
        updates = []
        tray.update_tray = lambda: updates.append("updated")

        self.assertFalse(tray.poll())
        self.assertIsNone(tray._poll_source_id)
        self.assertEqual(updates, [])

    def test_poll_does_not_write_icon_after_shutdown_during_query(self):
        device = FakeDevice()
        icon = FakePystrayIcon()
        icon.visible = True
        tray = object.__new__(StrixTrayPystray)
        tray.device = device
        tray.icon = icon
        tray._glib = None
        tray._main_loop = None
        tray._poll_source_id = 1
        tray._shutting_down = False
        device.after_query = tray.quit

        self.assertFalse(tray.poll())
        self.assertIsNone(tray._poll_source_id)
        self.assertEqual(icon.icon_write_threads, [])
        self.assertEqual(icon.title_write_threads, [])

    def test_quit_menu_item_uses_full_shutdown_path(self):
        tray = object.__new__(StrixTrayPystray)
        tray.icon = FakePystrayIcon()
        updates = []
        tray.update_tray = lambda: updates.append("updated")
        quit_calls = []
        tray.quit = lambda: quit_calls.append("quit")
        pystray = ModuleType("pystray")
        pystray.Menu = FakeMenu
        pystray.MenuItem = FakeMenuItem

        with patch.dict("sys.modules", {"pystray": pystray}):
            menu = tray.create_menu()

        refresh_item = find_menu_item(menu.items, "Refresh Now")
        refresh_item.action()
        self.assertEqual(updates, ["updated"])

        quit_item = find_menu_item(menu.items, "Quit")
        quit_item.action()
        self.assertEqual(quit_calls, ["quit"])
        self.assertEqual(tray.icon.stop_count, 0)


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
            patch("main.glob.glob", return_value=["/dev/hidraw0"]),
            patch("main.os.open", return_value=7),
            patch("main.fcntl.ioctl", side_effect=_ioctl),
            patch("main.time.sleep"),
            patch("main.os.close"),
            patch.object(device, "notify"),
            patch(
                "main.noise_reduction_state_path",
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
            "main.noise_reduction_state_path",
            return_value=pathlib.Path(tmp.name) / "noise-reduction.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_new_state_is_recorded_on_the_device(self):
        device = StrixDevice()
        with (
            patch("main.glob.glob", return_value=["/dev/hidraw0"]),
            patch("main.os.open", return_value=7),
            patch("main.fcntl.ioctl", side_effect=asus_ioctl()),
            patch("main.time.sleep"),
            patch("main.os.close"),
            patch.object(device, "notify"),
        ):
            device.set_noise_reduction(True)
            self.assertTrue(device.noise_reduction)

            device.set_noise_reduction(False)
            self.assertFalse(device.noise_reduction)

    def test_a_missing_receiver_fails_without_claiming_a_state_change(self):
        device = StrixDevice()
        with patch("main.glob.glob", return_value=[]):
            self.assertFalse(device.set_noise_reduction(True))
        self.assertFalse(device.noise_reduction)

    def test_a_denied_node_fails_without_claiming_a_state_change(self):
        device = StrixDevice()
        with (
            patch("main.glob.glob", return_value=["/dev/hidraw0"]),
            patch("main.os.open", side_effect=PermissionError(13, "Permission denied")),
        ):
            self.assertFalse(device.set_noise_reduction(True))
        self.assertFalse(device.noise_reduction)

    def test_an_ioctl_failure_fails_without_claiming_a_state_change(self):
        device = StrixDevice()
        with (
            patch("main.glob.glob", return_value=["/dev/hidraw0"]),
            patch("main.os.open", return_value=7),
            patch("main.fcntl.ioctl", side_effect=OSError(19, "No such device")),
            patch("main.os.close"),
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
            with patch("main.noise_reduction_state_path", return_value=path):
                device = StrixDevice()
                device.load_noise_reduction_state()
            self.assertTrue(device.noise_reduction)

    def test_a_missing_file_leaves_the_state_off(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            with patch(
                "main.noise_reduction_state_path", return_value=tmp / "absent.json"
            ):
                device = StrixDevice()
                device.load_noise_reduction_state()
            self.assertFalse(device.noise_reduction)

    def test_garbage_json_leaves_the_state_off(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = self.write_state(tmp, "{not json at all")
            with patch("main.noise_reduction_state_path", return_value=path):
                device = StrixDevice()
                device.load_noise_reduction_state()
            self.assertFalse(device.noise_reduction)

    def test_a_saved_non_boolean_leaves_the_state_off(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = self.write_state(tmp, json.dumps({"noise_reduction": "yes"}))
            with patch("main.noise_reduction_state_path", return_value=path):
                device = StrixDevice()
                device.load_noise_reduction_state()
            self.assertFalse(device.noise_reduction)

    def test_the_state_is_written_as_a_boolean_after_a_successful_send(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = tmp / "noise_reduction.json"
            device = StrixDevice()
            with (
                patch("main.glob.glob", return_value=["/dev/hidraw0"]),
                patch("main.os.open", return_value=7),
                patch("main.fcntl.ioctl", side_effect=asus_ioctl()),
                patch("main.time.sleep"),
                patch("main.os.close"),
                patch.object(device, "notify"),
                patch("main.noise_reduction_state_path", return_value=path),
            ):
                device.set_noise_reduction(True)
            self.assertEqual(json.loads(path.read_text()), {"noise_reduction": True})

    def test_a_failed_send_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            path = tmp / "noise_reduction.json"
            device = StrixDevice()
            with patch("main.glob.glob", return_value=[]):
                device.set_noise_reduction(True)
            self.assertFalse(path.exists())


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
            patch.object(main, "StrixDevice", return_value=device),
            patch.object(device, "set_noise_reduction", return_value=True) as setter,
        ):
            code, out = self.run_main(["--noise-reduction", "on"])
        setter.assert_called_once_with(True)
        self.assertIn("noise reduction on", out[0].lower())
        self.assertIsNone(code)

    def test_off_turns_it_off(self):
        device = StrixDevice()
        with (
            patch.object(main, "StrixDevice", return_value=device),
            patch.object(device, "set_noise_reduction", return_value=True) as setter,
        ):
            self.run_main(["--noise-reduction", "off"])
        setter.assert_called_once_with(False)

    def test_a_failed_send_reports_the_receiver_and_exits_one(self):
        device = StrixDevice()
        with (
            patch.object(main, "StrixDevice", return_value=device),
            patch.object(device, "set_noise_reduction", return_value=False),
        ):
            code, out = self.run_main(["--noise-reduction", "on"])
        self.assertEqual(code, 1)
        self.assertIn("could not be reached", out[0])

    def test_an_unknown_word_is_rejected_before_any_send(self):
        device = StrixDevice()
        with (
            patch.object(sys, "argv", ["strix-battery", "--noise-reduction", "maybe"]),
            patch.object(main, "StrixDevice", return_value=device),
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
            "main.noise_reduction_state_path",
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


class PystrayNoiseReductionMenuTests(unittest.TestCase):
    """The Pystray fallback must match the PyQt6 menu."""

    def bare_device(self, noise_reduction):
        """A StrixDevice without running __init__, so no state file is
        touched. The status lambdas also read these, so set them."""
        device = StrixDevice.__new__(StrixDevice)
        device.noise_reduction = noise_reduction
        device.last_status = None
        device.last_error = None
        return device

    def test_the_status_text_reflects_the_device_state(self):
        tray = object.__new__(StrixTrayPystray)
        # Same idiom as the tray itself above: a device without
        # running __init__, so no state file is touched.
        device = self.bare_device(True)
        tray.device = device
        self.assertIn("On", tray.get_noise_reduction_text())

    def find_toggle_item(self, menu):
        """The toggle is the item whose text is computed from the state."""
        found = []

        def walk(items):
            for item in items:
                if getattr(item, "submenu", None) is not None:
                    walk(item.submenu.items)
                elif callable(getattr(item, "text", None)) and (
                    "Microphone Noise Reduction" in item.text(None)
                ):
                    found.append(item)

        walk(menu.items)
        return found[0] if found else None

    def build_menu(self, tray):
        # create_menu imports pystray at call time; point it at the
        # same fakes the other menu tests use so the built menu can be
        # introspected (real pystray keeps its action callable private).
        pystray = ModuleType("pystray")
        pystray.Menu = FakeMenu
        pystray.MenuItem = FakeMenuItem

        with patch.dict("sys.modules", {"pystray": pystray}):
            return tray.create_menu()

    def test_the_toggle_item_text_reflects_the_device_state(self):
        tray = object.__new__(StrixTrayPystray)
        device = self.bare_device(True)
        tray.device = device
        menu = self.build_menu(tray)
        toggle = self.find_toggle_item(menu)
        self.assertIsNotNone(toggle)
        self.assertIn("On", toggle.text(None))

    def test_clicking_the_toggle_sends_the_opposite_state(self):
        tray = object.__new__(StrixTrayPystray)
        device = self.bare_device(False)
        tray.device = device
        menu = self.build_menu(tray)
        toggle = self.find_toggle_item(menu)
        self.assertIsNotNone(toggle)
        with patch.object(device, "set_noise_reduction", return_value=True) as setter:
            toggle.action()
        setter.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
