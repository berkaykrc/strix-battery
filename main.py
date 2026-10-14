#!/usr/bin/env python3
import argparse
import json
import os
import signal
import sys

from battery import (  # noqa: F401  - re-exported; `main` is the documented entry point
    DEFAULT_DISCHARGE_CURVE,
    DISCHARGE_CURVE_ENV_VAR,
    load_discharge_curve,
    mv_to_percent,
    parse_discharge_curve,
)
from device import (  # noqa: F401  - re-exported; `main` is the documented entry point
    BATTERY_DISPLAY_DEADBAND_PERCENT,
    ERROR_OFFLINE,
    ERROR_PERMISSION,
    HIDIOCGFEATURE_64,
    HIDIOCGRAWINFO,
    HIDIOCSFEATURE_64,
    MAX_SLEEP_MINUTES,
    NOTIFY_TIMEOUT_SECONDS,
    STRIX_PIDS,
    StrixDevice,
    noise_reduction_state_path,
    offline_message,
    validate_sleep_minutes,
)

# Tray glyphs as ASCII escapes so the source stays copy-pasteable and greppable.
HEADSET_ICON = "\U0001f3a7"
SLEEP_ICON = "\u23f1\ufe0f"
NOISE_REDUCTION_ICON = "\U0001f507"


def render_tray_icon(percentage: int, charging: bool, connected: bool):
    """Draw a high-contrast headset and battery tray icon."""
    from PIL import Image, ImageDraw, ImageFont

    size = (128, 128)
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if not connected:
        draw.arc(
            [24, 16, 104, 96], start=180, end=0, fill=(130, 130, 130, 200), width=10
        )
        draw.rounded_rectangle([16, 44, 38, 92], radius=8, fill=(100, 100, 100, 200))
        draw.rounded_rectangle([90, 44, 112, 92], radius=8, fill=(100, 100, 100, 200))
        draw.line([20, 20, 108, 108], fill=(230, 50, 50, 240), width=10)
        return img

    if charging:
        theme_color = (0, 190, 255)  # Charging: neon cyan
    elif percentage > 40:
        theme_color = (60, 220, 70)  # >40%: vivid green
    elif percentage > 15:
        theme_color = (255, 175, 20)  # 16-40%: orange
    else:
        theme_color = (240, 50, 50)  # <=15%: critical red

    draw.arc([22, 14, 106, 98], start=180, end=0, fill=(20, 20, 20, 240), width=14)
    draw.arc([24, 16, 104, 96], start=180, end=0, fill=(240, 240, 240, 255), width=10)

    draw.rounded_rectangle([14, 42, 40, 94], radius=10, fill=(20, 20, 20, 240))
    draw.rounded_rectangle([17, 45, 37, 91], radius=7, fill=theme_color)

    draw.rounded_rectangle([88, 42, 114, 94], radius=10, fill=(20, 20, 20, 240))
    draw.rounded_rectangle([91, 45, 111, 91], radius=7, fill=theme_color)

    draw.rounded_rectangle(
        [20, 96, 102, 122], radius=6, outline=(20, 20, 20, 240), width=4
    )
    draw.rounded_rectangle(
        [22, 98, 100, 120], radius=5, outline=(240, 240, 240, 255), width=2
    )
    draw.rectangle([102, 104, 108, 114], fill=(240, 240, 240, 255))

    fill_width = int(72 * (percentage / 100))
    if fill_width > 0:
        draw.rounded_rectangle(
            [25, 101, 25 + fill_width, 117], radius=3, fill=theme_color
        )

    if charging:
        bolt_points = [(68, 28), (50, 58), (64, 58), (58, 88), (80, 52), (66, 52)]
        draw.polygon(bolt_points, fill=(0, 190, 255))
        inner_bolt = [(x + (1 if x < 64 else -1), y) for x, y in bolt_points]
        draw.polygon(inner_bolt, fill=(255, 255, 255))
    else:
        font = None
        font_paths = [
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
            "/usr/share/fonts/cantarell/Cantarell-VF.otf",
            "/usr/share/fonts/google-noto/NotoSans-Bold.ttf",
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                try:
                    font = ImageFont.truetype(fp, 36)
                    break
                except (OSError, ValueError):
                    pass
        if not font:
            font = ImageFont.load_default()

        txt = f"{percentage}"
        bbox = draw.textbbox((0, 0), txt, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (128 - tw) // 2
        ty = 42 + (40 - th) // 2

        for ox, oy in [
            (-2, 0),
            (2, 0),
            (0, -2),
            (0, 2),
            (-1, -1),
            (1, 1),
            (-1, 1),
            (1, -1),
        ]:
            draw.text((tx + ox, ty + oy), txt, fill=(0, 0, 0, 255), font=font)
        draw.text((tx, ty), txt, fill=(255, 255, 255, 255), font=font)

    return img


def pil_to_qpixmap(pil_img):
    """Convert a Pillow image directly to a Qt QPixmap."""
    from PyQt6.QtGui import QImage, QPixmap

    img = pil_img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    qimg = QImage(data, img.width, img.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg)


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
    except ImportError:
        # The only import that can fail here is PyQt6 itself: pillow ships in
        # the base dependencies and a tray needs it to draw the icon.
        print("\nError: 'PyQt6' is required for tray mode.")
        print("Install it by running:")
        print("  uv add PyQt6 pillow\n")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="ASUS ROG Strix Go 2.4 - Battery and sleep manager"
    )
    parser.add_argument(
        "-t", "--tray", action="store_true", help="Start the system tray application"
    )
    parser.add_argument(
        "-j",
        "--json",
        action="store_true",
        help="Print JSON output for Waybar or desktop panels",
    )
    parser.add_argument(
        "-s",
        "--set-sleep",
        type=int,
        metavar="MINUTES",
        help=f"Set the automatic sleep timer (0-{MAX_SLEEP_MINUTES} minutes; 0 = disabled)",
    )
    parser.add_argument(
        "-n",
        "--noise-reduction",
        choices=["on", "off"],
        metavar="STATE",
        help="Set the microphone AI noise reduction (on or off)",
    )
    args = parser.parse_args()

    device = StrixDevice()

    if args.tray:
        start_tray(device)
        return

    if args.set_sleep is not None:
        try:
            validate_sleep_minutes(args.set_sleep)
        except ValueError as exc:
            parser.error(str(exc))

        success = device.set_sleep(args.set_sleep)
        if success:
            print(f"Sleep timer set to {args.set_sleep} minutes.")
        else:
            print("Error: Headset receiver could not be reached.")
            sys.exit(1)
        return

    if args.noise_reduction is not None:
        enabled = args.noise_reduction == "on"
        if device.set_noise_reduction(enabled):
            print(
                "Microphone noise reduction on."
                if enabled
                else "Microphone noise reduction off."
            )
        else:
            print("Error: Headset receiver could not be reached.")
            sys.exit(1)
        return

    st = device.query()

    if args.json:
        if not st:
            print(json.dumps({"status": "offline", "text": "Headset offline"}))
        else:
            charge_str = " (Charging)" if st["charging"] else ""
            print(
                json.dumps(
                    {
                        "status": "online",
                        "percentage": st["percentage"],
                        "voltage": st["voltage"],
                        "charging": st["charging"],
                        "sleep_min": st["sleep_min"],
                        "text": f"%{st['percentage']}{charge_str}",
                    }
                )
            )
        return

    if not st:
        print("ROG Strix Go 2.4 receiver or headset is offline/out of range.")
        sys.exit(1)

    state = "Charging" if st["charging"] else "On battery"
    print("--- ROG Strix Go 2.4 Status ---")
    print(f"Battery: {st['percentage']}% ({st['voltage']} mV)")
    print(f"Power status: {state}")
    print(f"Sleep timer: {st['sleep_min']} minutes")
    print(f"Device node: {st['path']}")


if __name__ == "__main__":
    main()
