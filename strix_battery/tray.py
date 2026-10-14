import importlib.util
import signal
import sys

from .device import StrixDevice, offline_message
from .icon import pil_to_qpixmap, render_tray_icon

# Tray glyphs as ASCII escapes so the source stays copy-pasteable and greppable.
HEADSET_ICON = "\U0001f3a7"
SLEEP_ICON = "\u23f1\ufe0f"
NOISE_REDUCTION_ICON = "\U0001f507"

# `start_tray` fails in two distinct ways and they need different advice.
# A missing PyQt6 is fixed by installing it. A PyQt6 that is installed but
# raises on import is a broken Qt install or a missing platform plugin, and
# telling that user to install it again changes nothing.
PYQT6_MISSING_HINT = (
    "\nError: 'PyQt6' is required for tray mode.\n"
    "Install it by running:\n"
    "  uv add PyQt6 pillow\n"
)

PYQT6_UNUSABLE_HINT = (
    "\nError: 'PyQt6' is installed but could not be used for tray mode.\n"
    "That means the distribution is present, but its import did not succeed.\n"
    "The release below names the module that actually failed to import,\n"
    "which may be PyQt6 itself or a dependency it pulls in.\n"
    "Cause: {error}\n"
)


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


def run_pyqt_tray(device: StrixDevice):
    """Run a native Qt6 tray application through Linux DBus/StatusNotifierItem."""
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QAction, QIcon
    from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(sys.argv)
    QApplication.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        # Without a StatusNotifierItem host the icon never appears and the
        # process would sit in app.exec() forever with no window and no menu.
        # Exit rather than continue: there is no second backend to try, and a
        # headless process would look like a working tray to the user.
        print(
            "Error: no system tray was found on this desktop.\n"
            "Qt could not reach a StatusNotifierItem host, so the tray icon "
            "would never appear.\n"
            "On GNOME, install the AppIndicator extension; on KDE Plasma, "
            "enable the Status Notifier extra.\n"
        )
        sys.exit(1)

    tray = QSystemTrayIcon()
    menu = QMenu()

    status_action = QAction("ROG Strix Go: Starting...", menu)
    status_action.setEnabled(False)
    menu.addAction(status_action)

    sleep_action = QAction("Sleep Timer: -", menu)
    sleep_action.setEnabled(False)
    menu.addAction(sleep_action)

    # A single two-state button: its label shows the current state, so
    # no separate status row is needed, and one click flips it.
    noise_toggle_action = QAction(
        f"{NOISE_REDUCTION_ICON} Microphone Noise Reduction: -", menu
    )
    noise_toggle_action.triggered.connect(
        lambda checked=False: (
            device.set_noise_reduction(not device.noise_reduction),
            update_state(),
        )
    )
    menu.addAction(noise_toggle_action)

    menu.addSeparator()

    sleep_menu = QMenu("Automatic Sleep Timer", menu)
    for mins, label in [
        (0, "Never Turn Off (0 min)"),
        (5, "5 Minutes"),
        (10, "10 Minutes"),
        (15, "15 Minutes"),
        (30, "30 Minutes"),
    ]:
        action = QAction(label, sleep_menu)
        action.triggered.connect(
            lambda checked=False, m=mins: (device.set_sleep(m), update_state())
        )
        sleep_menu.addAction(action)
    menu.addMenu(sleep_menu)

    menu.addSeparator()

    refresh_action = QAction("Refresh Now", menu)
    refresh_action.triggered.connect(lambda: update_state())
    menu.addAction(refresh_action)

    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)

    def update_state():
        st = device.query()
        if st:
            pct = st["percentage"]
            chg = st["charging"]
            v = st["voltage"]
            slp = st["sleep_min"]
            power_status = "Charging" if chg else "On battery"

            status_action.setText(f"🎧 Battery: {pct}% ({v} mV - {power_status})")
            sleep_action.setText(f"⏱️ Sleep Timer: {slp} minutes")
            noise_toggle_action.setText(
                f"{NOISE_REDUCTION_ICON} Microphone Noise Reduction: "
                f"{'On' if device.noise_reduction else 'Off'}"
            )
            tooltip = f"ROG Strix Go 2.4: {pct}% ({power_status})\nVoltage: {v} mV\nSleep timer: {slp} min"
            icon_img = render_tray_icon(pct, chg, connected=True)
        else:
            message = offline_message(device.last_error)
            status_action.setText(f"{HEADSET_ICON} {message}")
            sleep_action.setText(f"{SLEEP_ICON} Sleep Timer: -")
            noise_toggle_action.setText(
                f"{NOISE_REDUCTION_ICON} Microphone Noise Reduction: "
                f"{'On' if device.noise_reduction else 'Off'}"
            )
            tooltip = message
            icon_img = render_tray_icon(0, False, connected=False)

        qpix = pil_to_qpixmap(icon_img)
        tray.setIcon(QIcon(qpix))
        tray.setToolTip(tooltip)

    refresh_timer = QTimer()

    def poll():
        # Desktop environments hide the tray icon when a fullscreen menu opens.
        # Querying the device every 15 seconds while nothing is on screen is
        # pure waste, so skip until the icon is back. The guard belongs here
        # and not in update_state: initialize_tray refreshes before show().
        if not tray.isVisible():
            return
        update_state()

    def initialize_tray():
        update_state()
        tray.show()

        refresh_timer.timeout.connect(poll)
        refresh_timer.start(15000)

    sys.exit(_run_qt_app_with_sigint_shutdown(app, QTimer, initialize_tray))


def start_tray(device: StrixDevice):
    """Run the PyQt6 tray, or explain that it is unavailable."""
    try:
        run_pyqt_tray(device)
    except ImportError as exc:
        # `find_spec` checks presence, not importability: a PyQt6 that is
        # installed but broken still produces a spec, and that user needs
        # different advice from one who never installed it.
        try:
            missing = importlib.util.find_spec("PyQt6") is None
        except ValueError:
            # find_spec raises when the name is in sys.modules without a spec
            # (a fake, or a half-finished import). "Cannot tell" is not a clean
            # missing install, so it gets the unusable advice too.
            missing = False
        if missing:
            print(PYQT6_MISSING_HINT)
        else:
            print(PYQT6_UNUSABLE_HINT.format(error=exc))
        sys.exit(1)
