#!/usr/bin/env python3
import argparse
import contextlib
import fcntl
import glob
import itertools
import json
import os
import pathlib
import signal
import struct
import subprocess
import sys
import time

# Linux hidraw ioctl codes (64-byte payload)
HIDIOCGRAWINFO = 0x80084803
HIDIOCSFEATURE_64 = 0xC0404806
HIDIOCGFEATURE_64 = 0xC0404807

ASUS_VID = 0x0B05
STRIX_PIDS = [0x18D6, 0x18D7]  # 0x18D6: Kablosuz Dongle, 0x18D7: Kablolu Mod
MAX_SLEEP_MINUTES = 1092

# Why the last query produced no status. Distinguishing these lets the tray
# tell the user to fix their udev rules instead of blaming the headset.
ERROR_OFFLINE = "offline"
ERROR_PERMISSION = "permission"

# Tray glyphs as ASCII escapes so the source stays copy-pasteable and greppable.
HEADSET_ICON = "\U0001f3a7"
SLEEP_ICON = "\u23f1\ufe0f"
NOISE_REDUCTION_ICON = "\U0001f507"

# `notify-send` runs on the Qt/GLib main thread, so a hung notification daemon
# would freeze the event loop with no way out. Bound the wait.
NOTIFY_TIMEOUT_SECONDS = 3

# Voltage readings wobble by a few millivolts between polls and `mv_to_percent`
# is a step function, so reporting the raw value makes the tray icon flicker.
# Only report a new percentage once it has genuinely moved. Raising this to 3
# calms the icon further at the cost of coarser steps.
BATTERY_DISPLAY_DEADBAND_PERCENT = 2

# Notify at 15% or below, but only re-arm for a fresh alert above 20%, so a
# battery resting in between does not notify on every poll.
LOW_BATTERY_NOTIFY_PERCENT = 15
LOW_BATTERY_REARM_PERCENT = 20

# 64-byte status query packet
QUERY_PACKET = bytearray(
    [0xFF, 0x08, 0x00, 0xFD, 0x04, 0x12, 0xF1, 0x03, 0x52, 0x01] + [0x00] * 54
)

# Mic noise reduction, from the packet G-Helper 0.286 sends for this headset:
# FF 08 00 FF 04 12 F1 03 52 <0C off | 0D on>. Byte 9 is the only field that
# changes between the two states, so keep the mapping in one place rather than
# branching at the call site. Both values confirmed by live listening test:
# on removes background noise, off brings it back.
NOISE_REDUCTION_SUB_COMMAND = {False: 0x0C, True: 0x0D}


# The firmware will not report the current noise-reduction state, so the last
# value we sent is the only record of it. Keep it beside the other user
# settings rather than in the working directory.
def noise_reduction_state_path():
    return pathlib.Path.home() / ".config" / "strix-battery" / "noise-reduction.json"


def validate_sleep_minutes(minutes: int) -> int:
    """Validate a duration that fits the packet's 16-bit seconds field."""
    if not 0 <= minutes <= MAX_SLEEP_MINUTES:
        raise ValueError(
            f"Sleep duration must be between 0 and {MAX_SLEEP_MINUTES} minutes."
        )
    return minutes


# Millivolt -> percentage calibration points, ordered by rising voltage.
#
# THIS IS THE LINEAR MAP, NOT A MEASUREMENT. It is what the code did before the
# curve became a table, kept as the default so this change is provably
# behaviour-preserving. A real Li-ion cell is NOT linear: its mid-range sits
# well above a straight line between the endpoints, so a full discharge
# understates the middle badly (a 3900mV cell is roughly 75-80%, not 70%).
#
# Correcting this requires measurements from the actual cell, because the
# right table depends on the chemistry, the load, and the temperature. Do not
# replace it with a table copied from another cell or from a generic curve: an
# unverified table looks authoritative and is not. See
# docs/superpowers/plans/2026-09-28-discharge-curve.md, Task 2.
#
# Override without editing code: STRIX_BATTERY_CURVE="3300:0,3600:25,4150:100"
DEFAULT_DISCHARGE_CURVE = ((3300, 0), (4150, 100))

DISCHARGE_CURVE_ENV_VAR = "STRIX_BATTERY_CURVE"

# Specs already warned about, so a typo is reported once rather than on every
# poll. Both tray backends poll every 15s; without this the same line lands in
# the journal thousands of times a day and buries itself.
_WARNED_CURVES: set[str] = set()


def parse_discharge_curve(spec: str) -> tuple[tuple[int, int], ...]:
    """Parse "mv:pct,mv:pct,..." into an ordered tuple of pairs.

    Raises ValueError on anything malformed, including a curve that is not
    strictly ascending in voltage or that leaves the 0-100 range. A bad
    override must fail loudly here rather than produce a nonsense readout.
    """
    points = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise ValueError(f"empty point in {spec!r}")
        mv_text, _, pct_text = chunk.partition(":")
        try:
            mv, pct = int(mv_text), int(pct_text)
        except ValueError:
            raise ValueError(f"bad point {chunk!r} in {spec!r}") from None
        if not 0 <= pct <= 100:
            raise ValueError(f"percentage {pct} out of range in {spec!r}")
        points.append((mv, pct))

    if len(points) < 2:
        raise ValueError(f"a curve needs at least two points, got {spec!r}")
    for prev, point in itertools.pairwise(points):
        prev_mv, prev_pct = prev
        mv, pct = point
        if mv <= prev_mv:
            raise ValueError(f"voltage must ascend: {prev_mv} then {mv}")
        if pct < prev_pct:
            raise ValueError(f"percentage must not fall: {prev_pct} then {pct}")
    return tuple(points)


def load_discharge_curve() -> tuple[tuple[int, int], ...]:
    """The calibrated curve, or the shipped one with a warning if the override is bad."""
    spec = os.environ.get(DISCHARGE_CURVE_ENV_VAR)
    if not spec:
        return DEFAULT_DISCHARGE_CURVE
    try:
        return parse_discharge_curve(spec)
    except ValueError as exc:
        if spec not in _WARNED_CURVES:
            _WARNED_CURVES.add(spec)
            print(
                f"Warning: ignoring {DISCHARGE_CURVE_ENV_VAR}={spec!r}: {exc}",
                file=sys.stderr,
            )
        return DEFAULT_DISCHARGE_CURVE


def mv_to_percent(mv: int, curve: tuple[tuple[int, int], ...] | None = None) -> int:
    """Convert 1S Li-ion battery millivolts to a percentage from 0 to 100.

    Interpolates linearly between calibration points and clamps outside the
    table. `curve` exists so the tests can exercise a multi-point table without
    mutating the process environment.
    """
    points = load_discharge_curve() if curve is None else curve

    if mv <= points[0][0]:
        return points[0][1]
    if mv >= points[-1][0]:
        return points[-1][1]

    for low, high in itertools.pairwise(points):
        low_mv, low_pct = low
        high_mv, high_pct = high
        if low_mv <= mv <= high_mv:
            span = high_mv - low_mv
            offset = (mv - low_mv) / span
            return int(low_pct + offset * (high_pct - low_pct))

    # Unreachable: the range checks above cover every point. Kept so a
    # malformed curve returns something bounded rather than raising.
    return points[-1][1]


def offline_message(last_error: str | None) -> str:
    """Explain why no headset answered, without blaming one unproven cause.

    `ERROR_PERMISSION` means a hidraw node denied access *while scanning*, not
    that this is why the headset is unreachable. Plenty of hosts own a
    root:root 0660 hidraw node for a built-in laptop HID device, and the udev
    rule in the README only matches 0b05:18d6/18d7, so such a node is denied
    even on a correctly configured machine. Reporting that as the cause sends a
    user who already installed the rule to install it again, so the permission
    case is a hint appended to the offline diagnosis, never a replacement for it.
    """
    message = "Headset: Offline / Out of range"
    if last_error == ERROR_PERMISSION:
        message += (
            " (a /dev/hidraw* node also denied permission -"
            " check the udev rules in the README)"
        )
    return message


class StrixDevice:
    """Manage USB HID communication with the ROG Strix Go 2.4."""

    def __init__(self):
        self.last_status = None
        self.notified_low = False
        self.last_error = None
        self._permission_denied = False
        self._displayed_percentage = None
        self.noise_reduction = False
        self.load_noise_reduction_state()

    def stabilize_percentage(self, percentage: int) -> int:
        """Hold the last reported percentage until it genuinely moves.

        The hold only suppresses changes smaller than the deadband. A real
        discharge produces a much larger step and is reported immediately, so
        this costs no accuracy while removing the flicker that plain
        millivolt noise causes.
        """
        if (
            self._displayed_percentage is None
            or abs(percentage - self._displayed_percentage)
            >= BATTERY_DISPLAY_DEADBAND_PERCENT
        ):
            self._displayed_percentage = percentage
        return self._displayed_percentage

    def _go_offline(self, error: str = ERROR_OFFLINE):
        """Record that no headset answered and drop the display hold."""
        self.last_status = None
        self.last_error = error
        self._displayed_percentage = None

    def _update_low_battery_notice(self, percentage: int, charging: bool) -> bool:
        """Notify once when critically low; return the new latch state.

        Split out from `query` so the 15% trigger and the 20% re-arm point can
        be tested without hardware, and so the latch logic is readable on its
        own.
        """
        if (
            percentage <= LOW_BATTERY_NOTIFY_PERCENT
            and not charging
            and not self.notified_low
        ):
            self.notify("ROG Strix Go 2.4", f"Battery critically low: {percentage}%!")
            self.notified_low = True
        elif percentage > LOW_BATTERY_REARM_PERCENT:
            self.notified_low = False
        return self.notified_low

    def find_node(self):
        """Find the ASUS ROG Strix Go device and return its file descriptor."""
        permission_denied = False
        for path in sorted(glob.glob("/dev/hidraw*")):
            try:
                fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            except PermissionError:
                # The udev rule from the README is missing. Keep scanning: a
                # different node may still be the receiver.
                permission_denied = True
                continue
            except OSError:
                continue

            try:
                buf = bytearray(8)
                fcntl.ioctl(fd, HIDIOCGRAWINFO, buf)
                _, vid, pid = struct.unpack("<Ihh", buf)
                if (vid & 0xFFFF) == ASUS_VID and (pid & 0xFFFF) in STRIX_PIDS:
                    # We found it, so a denied node elsewhere is irrelevant.
                    self._permission_denied = False
                    return fd, path
                os.close(fd)
            except (OSError, struct.error):
                os.close(fd)
                continue

        self._permission_denied = permission_denied
        return None, None

    def query(self):
        """Read the headset battery, voltage, charging, and sleep timer status."""
        fd, path = self.find_node()
        if fd is None:
            self._go_offline(
                ERROR_PERMISSION if self._permission_denied else ERROR_OFFLINE
            )
            return None

        try:
            # Send the status query.
            fcntl.ioctl(fd, HIDIOCSFEATURE_64, QUERY_PACKET)
            time.sleep(0.04)

            # Read the response packet (report ID: 0xFF).
            buf = bytearray([0xFF] + [0x00] * 63)
            fcntl.ioctl(fd, HIDIOCGFEATURE_64, buf)

            # A powered-off or disconnected headset returns zeroed bytes.
            if buf[1] != 0x1B:
                self._go_offline()
                return None

            voltage_mv = (buf[12] << 8) | buf[11]
            pct = self.stabilize_percentage(mv_to_percent(voltage_mv))
            charging = buf[9] == 0x0A
            sleep_sec = (buf[22] << 8) | buf[21]

            status = {
                "percentage": pct,
                "voltage": voltage_mv,
                "charging": charging,
                "sleep_min": sleep_sec // 60,
                "path": path,
            }
            self.last_status = status
            self.last_error = None

            self._update_low_battery_notice(pct, charging)

            return status
        except OSError:
            self._go_offline()
            return None
        finally:
            os.close(fd)

    def set_sleep(self, minutes: int):
        """Set the automatic sleep timer (0 = never turn off)."""
        try:
            validate_sleep_minutes(minutes)
        except ValueError:
            return False

        fd, _ = self.find_node()
        if fd is None:
            return False

        try:
            seconds = minutes * 60
            sec_lsb = seconds & 0xFF
            sec_msb = (seconds >> 8) & 0xFF

            payload = bytearray(
                [
                    0xFF,
                    0x0A,
                    0x00,
                    0xFF,
                    0x04,
                    0x12,
                    0xF1,
                    0x05,
                    0x52,
                    0x0B,
                    sec_lsb,
                    sec_msb,
                ]
                + [0x00] * 52
            )

            fcntl.ioctl(fd, HIDIOCSFEATURE_64, payload)
            self.notify("ROG Strix Go", f"Sleep timer set to {minutes} minutes.")
            self.query()
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def set_noise_reduction(self, enabled: bool):
        """Toggle the microphone AI noise reduction in the headset firmware.

        The firmware accepts the report even while the headset is
        asleep, so a successful send does not confirm the headset
        applied the setting: the state file can lead the firmware.
        """
        fd, _ = self.find_node()
        if fd is None:
            return False

        try:
            payload = bytearray(
                [
                    0xFF,
                    0x08,
                    0x00,
                    0xFF,
                    0x04,
                    0x12,
                    0xF1,
                    0x03,
                    0x52,
                    NOISE_REDUCTION_SUB_COMMAND[bool(enabled)],
                ]
                + [0x00] * 54
            )

            fcntl.ioctl(fd, HIDIOCSFEATURE_64, payload)
            self.noise_reduction = bool(enabled)
            self.save_noise_reduction_state()
            self.notify(
                "ROG Strix Go",
                "Microphone noise reduction on."
                if enabled
                else "Microphone noise reduction off.",
            )
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def load_noise_reduction_state(self):
        """Restore the last noise-reduction value we sent.

        Anything unexpected in the file means "off": the firmware has no reset
        command, so refusing to start over a bad state file would be worse than
        guessing.
        """
        try:
            with noise_reduction_state_path().open(encoding="utf-8") as handle:
                stored = json.load(handle)
            self.noise_reduction = stored.get("noise_reduction") is True
        except (OSError, ValueError, AttributeError):
            self.noise_reduction = False

    def save_noise_reduction_state(self):
        """Persist the current value, creating the directory on first use."""
        try:
            path = noise_reduction_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                json.dump({"noise_reduction": self.noise_reduction}, handle)
        except OSError:
            pass

    def notify(self, title: str, msg: str):
        """Send a desktop notification through notify-send, bounded in time."""
        # notify-send may be missing (FileNotFoundError) or its daemon hung
        # (TimeoutExpired, a SubprocessError): either way, skip the note
        # rather than stalling the tray.
        with contextlib.suppress(FileNotFoundError, subprocess.SubprocessError):
            subprocess.run(
                ["notify-send", title, msg, "-i", "audio-headset"],
                check=False,
                timeout=NOTIFY_TIMEOUT_SECONDS,
            )


def render_tray_icon(percentage: int, charging: bool, connected: bool):
    """Draw a high-contrast headset and battery tray icon."""
    from PIL import Image, ImageDraw, ImageFont

    size = (128, 128)
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if not connected:
        # Draw a gray headset with a red cross when disconnected.
        draw.arc(
            [24, 16, 104, 96], start=180, end=0, fill=(130, 130, 130, 200), width=10
        )
        draw.rounded_rectangle([16, 44, 38, 92], radius=8, fill=(100, 100, 100, 200))
        draw.rounded_rectangle([90, 44, 112, 92], radius=8, fill=(100, 100, 100, 200))
        draw.line([20, 20, 108, 108], fill=(230, 50, 50, 240), width=10)
        return img

    # Select a color for the current battery state.
    if charging:
        theme_color = (0, 190, 255)  # Charging: neon cyan
    elif percentage > 40:
        theme_color = (60, 220, 70)  # >40%: vivid green
    elif percentage > 15:
        theme_color = (255, 175, 20)  # 16-40%: orange
    else:
        theme_color = (240, 50, 50)  # <=15%: critical red

    # 1. Headband (double layer for background contrast).
    draw.arc([22, 14, 106, 98], start=180, end=0, fill=(20, 20, 20, 240), width=14)
    draw.arc([24, 16, 104, 96], start=180, end=0, fill=(240, 240, 240, 255), width=10)

    # 2. Ear cushions.
    draw.rounded_rectangle([14, 42, 40, 94], radius=10, fill=(20, 20, 20, 240))
    draw.rounded_rectangle([17, 45, 37, 91], radius=7, fill=theme_color)

    draw.rounded_rectangle([88, 42, 114, 94], radius=10, fill=(20, 20, 20, 240))
    draw.rounded_rectangle([91, 45, 111, 91], radius=7, fill=theme_color)

    # 3. Lower section: battery capsule.
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

    # 4. Center: lightning bolt while charging or a large percentage on battery.
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

        # Add a thick black shadow to improve readability.
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

        # Her 15 saniyede bir otomatik sorgula
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

    # 1. Tepsi Modu
    if args.tray:
        start_tray(device)
        return

    # 2. Set the sleep timer.
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

    # 2b. Toggle the microphone noise reduction.
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

    # 3. Query status (CLI/JSON).
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

    # Default terminal report.
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
