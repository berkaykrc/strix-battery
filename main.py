#!/usr/bin/env python3
import argparse
import fcntl
import glob
import importlib.util
import json
import os
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

# 64-byte status query packet
QUERY_PACKET = bytearray([
    0xFF, 0x08, 0x00, 0xFD, 0x04, 0x12, 0xF1, 0x03, 0x52, 0x01
] + [0x00] * 54)


def _load_glib():
    from gi.repository import GLib
    return GLib


def validate_sleep_minutes(minutes: int) -> int:
    """Validate a duration that fits the packet's 16-bit seconds field."""
    if not 0 <= minutes <= MAX_SLEEP_MINUTES:
        raise ValueError(
            f"Sleep duration must be between 0 and {MAX_SLEEP_MINUTES} minutes."
        )
    return minutes


def mv_to_percent(mv: int) -> int:
    """Convert 1S Li-ion battery voltage to a percentage from 0 to 100."""
    if mv >= 4150:
        return 100
    if mv <= 3300:
        return 0
    return int((mv - 3300) / (4150 - 3300) * 100)


class StrixDevice:
    """Manage USB HID communication with the ROG Strix Go 2.4."""

    def __init__(self):
        self.last_status = None
        self.notified_low = False

    def find_node(self):
        """Find the ASUS ROG Strix Go device and return its file descriptor."""
        for path in sorted(glob.glob("/dev/hidraw*")):
            try:
                fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            except OSError:
                continue

            try:
                buf = bytearray(8)
                fcntl.ioctl(fd, HIDIOCGRAWINFO, buf)
                _, vid, pid = struct.unpack('<Ihh', buf)
                if (vid & 0xFFFF) == ASUS_VID and (pid & 0xFFFF) in STRIX_PIDS:
                    return fd, path
                os.close(fd)
            except (OSError, struct.error):
                os.close(fd)
                continue
        return None, None

    def query(self):
        """Read the headset battery, voltage, charging, and sleep timer status."""
        fd, path = self.find_node()
        if fd is None:
            self.last_status = None
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
                self.last_status = None
                return None

            voltage_mv = (buf[12] << 8) | buf[11]
            pct = mv_to_percent(voltage_mv)
            charging = (buf[9] == 0x0A)
            sleep_sec = (buf[22] << 8) | buf[21]

            status = {
                "percentage": pct,
                "voltage": voltage_mv,
                "charging": charging,
                "sleep_min": sleep_sec // 60,
                "path": path
            }
            self.last_status = status

            # Notify once when the battery reaches a critical level (15% or less).
            if pct <= 15 and not charging and not self.notified_low:
                self.notify("ROG Strix Go 2.4", f"Battery critically low: {pct}%!")
                self.notified_low = True
            elif pct > 20:
                self.notified_low = False

            return status
        except OSError:
            self.last_status = None
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

            payload = bytearray([
                0xFF, 0x0A, 0x00, 0xFF, 0x04, 0x12, 0xF1, 0x05, 0x52, 0x0B,
                sec_lsb, sec_msb
            ] + [0x00] * 52)

            fcntl.ioctl(fd, HIDIOCSFEATURE_64, payload)
            self.notify("ROG Strix Go", f"Sleep timer set to {minutes} minutes.")
            self.query()
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def notify(self, title: str, msg: str):
        """Send a desktop notification through notify-send."""
        try:
            subprocess.run(["notify-send", title, msg, "-i", "audio-headset"], check=False)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass


def render_tray_icon(percentage: int, charging: bool, connected: bool):
    """Draw a high-contrast headset and battery tray icon."""
    from PIL import Image, ImageDraw, ImageFont

    size = (128, 128)
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if not connected:
        # Draw a gray headset with a red cross when disconnected.
        draw.arc([24, 16, 104, 96], start=180, end=0, fill=(130, 130, 130, 200), width=10)
        draw.rounded_rectangle([16, 44, 38, 92], radius=8, fill=(100, 100, 100, 200))
        draw.rounded_rectangle([90, 44, 112, 92], radius=8, fill=(100, 100, 100, 200))
        draw.line([20, 20, 108, 108], fill=(230, 50, 50, 240), width=10)
        return img

    # Select a color for the current battery state.
    if charging:
        theme_color = (0, 190, 255)      # Charging: neon cyan
    elif percentage > 40:
        theme_color = (60, 220, 70)      # >40%: vivid green
    elif percentage > 15:
        theme_color = (255, 175, 20)     # 16-40%: orange
    else:
        theme_color = (240, 50, 50)      # <=15%: critical red

    # 1. Headband (double layer for background contrast).
    draw.arc([22, 14, 106, 98], start=180, end=0, fill=(20, 20, 20, 240), width=14)
    draw.arc([24, 16, 104, 96], start=180, end=0, fill=(240, 240, 240, 255), width=10)

    # 2. Ear cushions.
    draw.rounded_rectangle([14, 42, 40, 94], radius=10, fill=(20, 20, 20, 240))
    draw.rounded_rectangle([17, 45, 37, 91], radius=7, fill=theme_color)

    draw.rounded_rectangle([88, 42, 114, 94], radius=10, fill=(20, 20, 20, 240))
    draw.rounded_rectangle([91, 45, 111, 91], radius=7, fill=theme_color)

    # 3. Lower section: battery capsule.
    draw.rounded_rectangle([20, 96, 102, 122], radius=6, outline=(20, 20, 20, 240), width=4)
    draw.rounded_rectangle([22, 98, 100, 120], radius=5, outline=(240, 240, 240, 255), width=2)
    draw.rectangle([102, 104, 108, 114], fill=(240, 240, 240, 255))

    fill_width = int(72 * (percentage / 100))
    if fill_width > 0:
        draw.rounded_rectangle([25, 101, 25 + fill_width, 117], radius=3, fill=theme_color)

    # 4. Center: lightning bolt while charging or a large percentage on battery.
    if charging:
        bolt_points = [
            (68, 28), (50, 58), (64, 58),
            (58, 88), (80, 52), (66, 52)
        ]
        draw.polygon(bolt_points, fill=(0, 190, 255))
        inner_bolt = [(x + (1 if x < 64 else -1), y) for x, y in bolt_points]
        draw.polygon(inner_bolt, fill=(255, 255, 255))
    else:
        font = None
        font_paths = [
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
            "/usr/share/fonts/cantarell/Cantarell-VF.otf",
            "/usr/share/fonts/google-noto/NotoSans-Bold.ttf"
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
        for ox, oy in [(-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1), (-1, 1), (1, -1)]:
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


def run_pyqt_tray(device: StrixDevice):
    """Run a native Qt6 tray application through Linux DBus/StatusNotifierItem."""
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QAction, QIcon
    from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(sys.argv)
    QApplication.setQuitOnLastWindowClosed(False)

    tray = QSystemTrayIcon()
    menu = QMenu()

    status_action = QAction("ROG Strix Go: Starting...", menu)
    status_action.setEnabled(False)
    menu.addAction(status_action)

    sleep_action = QAction("Sleep Timer: -", menu)
    sleep_action.setEnabled(False)
    menu.addAction(sleep_action)

    menu.addSeparator()

    sleep_menu = QMenu("Automatic Sleep Timer", menu)
    for mins, label in [
        (0, "Never Turn Off (0 min)"),
        (5, "5 Minutes"),
        (10, "10 Minutes"),
        (15, "15 Minutes"),
        (30, "30 Minutes")
    ]:
        action = QAction(label, sleep_menu)
        action.triggered.connect(lambda checked=False, m=mins: (device.set_sleep(m), update_state()))
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
            tooltip = f"ROG Strix Go 2.4: {pct}% ({power_status})\nVoltage: {v} mV\nSleep timer: {slp} min"
            icon_img = render_tray_icon(pct, chg, connected=True)
        else:
            status_action.setText("🎧 Headset: Offline / Out of range")
            sleep_action.setText("⏱️ Sleep Timer: -")
            tooltip = "ROG Strix Go 2.4: Offline"
            icon_img = render_tray_icon(0, False, connected=False)

        qpix = pil_to_qpixmap(icon_img)
        tray.setIcon(QIcon(qpix))
        tray.setToolTip(tooltip)

    update_state()
    tray.show()

    # Her 15 saniyede bir otomatik sorgula
    timer = QTimer()
    timer.timeout.connect(update_state)
    timer.start(15000)

    sys.exit(app.exec())


class StrixTrayPystray:
    """Manage the Pystray-based fallback tray application."""

    def __init__(self, device: StrixDevice):
        import pystray
        self.device = device
        self.icon = pystray.Icon("strix_go")
        self.icon.menu = self.create_menu()
        self._glib = None
        self._main_loop = None
        self._poll_source_id = None
        self._shutting_down = False
        self.update_tray()

    def get_status_text(self):
        st = self.device.last_status
        if not st:
            return "Headset: Offline / Out of range"
        power_status = "Charging" if st["charging"] else "On battery"
        return f"Battery: {st['percentage']}% ({st['voltage']} mV - {power_status})"

    def get_sleep_text(self):
        st = self.device.last_status
        if not st:
            return "Sleep Timer: -"
        return f"Sleep Timer: {st['sleep_min']} minutes"

    def create_menu(self):
        from pystray import Menu
        from pystray import MenuItem as item
        return Menu(
            item(lambda text: self.get_status_text(), None, enabled=False),
            item(lambda text: self.get_sleep_text(), None, enabled=False),
            Menu.SEPARATOR,
            item("Automatic Sleep Timer", Menu(
                item("Never Turn Off (0 min)", lambda: self.device.set_sleep(0)),
                item("5 Minutes", lambda: self.device.set_sleep(5)),
                item("10 Minutes", lambda: self.device.set_sleep(10)),
                item("15 Minutes", lambda: self.device.set_sleep(15)),
                item("30 Minutes", lambda: self.device.set_sleep(30)),
            )),
            item("Refresh Now", lambda: self.update_tray()),
            Menu.SEPARATOR,
            item("Quit", lambda: self.quit())
        )

    def update_tray(self):
        if self._shutting_down:
            return
        st = self.device.query()
        if self._shutting_down:
            return
        if st:
            pct = st["percentage"]
            self.icon.icon = render_tray_icon(pct, st["charging"], connected=True)
            self.icon.title = f"ROG Strix Go: {pct}% ({'Charging' if st['charging'] else 'On battery'})"
        else:
            self.icon.icon = render_tray_icon(0, False, connected=False)
            self.icon.title = "ROG Strix Go: Offline"

    def quit(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        source_id = self._poll_source_id
        self._poll_source_id = None
        try:
            if self._glib is not None and source_id is not None:
                self._glib.source_remove(source_id)
        finally:
            try:
                self.icon.stop()
            finally:
                if self._main_loop is not None:
                    main_loop = self._main_loop
                    self._main_loop = None
                    main_loop.quit()

    def poll(self):
        if self._shutting_down or not self.icon.visible:
            self._poll_source_id = None
            return False
        try:
            self.update_tray()
        except Exception:
            self._poll_source_id = None
            raise
        if self._shutting_down:
            self._poll_source_id = None
            return False
        return True

    def start(self):
        self._shutting_down = False
        self._glib = None
        self._main_loop = None
        self._poll_source_id = None
        try:
            glib = _load_glib()
        except ImportError:
            print("Info: GLib is unavailable; automatic tray refresh is disabled.")
            try:
                self.icon.run()
            finally:
                self.quit()
            return

        self._glib = glib
        try:
            self._main_loop = glib.MainLoop()
            self.icon.run_detached()
            self._poll_source_id = glib.timeout_add_seconds(15, self.poll)
            self._main_loop.run()
        finally:
            self.quit()


def start_tray(device: StrixDevice):
    """Prefer PyQt6 and fall back to Pystray when necessary."""
    if importlib.util.find_spec("PyQt6") is not None:
        try:
            run_pyqt_tray(device)
            return
        except ImportError:
            print("Info: PyQt6 could not be used, starting with Pystray.")

    if importlib.util.find_spec("pystray") is not None and importlib.util.find_spec("PIL") is not None:
        print("Info: PyQt6 was not found, starting with Pystray.")
        print("For a smoother experience and native context menu, run: uv add PyQt6\n")
        app = StrixTrayPystray(device)
        app.start()
        return

    print("\nError: 'PyQt6' is required for tray mode.")
    print("Install it by running:")
    print("  uv add PyQt6 pillow\n")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="ASUS ROG Strix Go 2.4 - Battery and sleep manager"
    )
    parser.add_argument(
        "-t", "--tray",
        action="store_true",
        help="Start the system tray application"
    )
    parser.add_argument(
        "-j", "--json",
        action="store_true",
        help="Print JSON output for Waybar or desktop panels"
    )
    parser.add_argument(
        "-s", "--set-sleep",
        type=int,
        metavar="MINUTES",
        help=f"Set the automatic sleep timer (0-{MAX_SLEEP_MINUTES} minutes; 0 = disabled)"
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

    # 3. Query status (CLI/JSON).
    st = device.query()

    if args.json:
        if not st:
            print(json.dumps({"status": "offline", "text": "Headset offline"}))
        else:
            charge_str = " (Charging)" if st["charging"] else ""
            print(json.dumps({
                "status": "online",
                "percentage": st["percentage"],
                "voltage": st["voltage"],
                "charging": st["charging"],
                "sleep_min": st["sleep_min"],
                "text": f"%{st['percentage']}{charge_str}"
            }))
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