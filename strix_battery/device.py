import contextlib
import fcntl
import glob
import json
import os
import pathlib
import struct
import subprocess
import time

from .battery import mv_to_percent

# Linux hidraw ioctl codes (64-byte payload)
HIDIOCGRAWINFO = 0x80084803
HIDIOCSFEATURE_64 = 0xC0404806
HIDIOCGFEATURE_64 = 0xC0404807

ASUS_VID = 0x0B05
STRIX_PIDS = [0x18D6, 0x18D7]  # 0x18D6: wireless dongle, 0x18D7: wired mode
MAX_SLEEP_MINUTES = 1092

# Why the last query produced no status. Distinguishing these lets the tray
# tell the user to fix their udev rules instead of blaming the headset.
ERROR_OFFLINE = "offline"
ERROR_PERMISSION = "permission"

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

# 64-byte status query packet. `bytes`, not `bytearray`: this is a module-level
# constant handed straight to ioctl on every poll, so a stray single-byte write
# would corrupt every later query for the life of the process.
QUERY_PACKET = bytes(
    [0xFF, 0x08, 0x00, 0xFD, 0x04, 0x12, 0xF1, 0x03, 0x52, 0x01] + [0x00] * 54
)

# Byte offsets inside the 64-byte status reply. Named so a firmware revision
# that shifts one field shows up as a diff a human reads, instead of a user
# staring at a permanent "Offline" with no hint why. Voltage and sleep time
# are little-endian 16-bit values split across LSB and MSB.
REPLY_MARKER = 0x1B
OFFSET_CHARGING = 9
CHARGING_FLAG = 0x0A
OFFSET_VOLUME_LSB = 11
OFFSET_VOLUME_MSB = 12
OFFSET_SLEEP_LSB = 21
OFFSET_SLEEP_MSB = 22

# The sleep-timer command is a different packet from the status query, and its
# own seconds field. Kept separate from the read-path offsets above so nobody
# confuses the two.
SLEEP_PACKET_COMMAND = bytes(
    [0xFF, 0x0A, 0x00, 0xFF, 0x04, 0x12, 0xF1, 0x05, 0x52, 0x0B]
)
OFFSET_SLEEP_SECONDS_LSB = 10
OFFSET_SLEEP_SECONDS_MSB = 11

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


def build_sleep_packet(minutes: int) -> bytes:
    """Encode the sleep-timer command. Pure, so the encoding can be tested.

    The seconds field is 16 bits, which is why MAX_SLEEP_MINUTES is 1092.
    The check is a backstop: if the cap is ever relaxed, an overflowing
    duration would wrap into a timer set to a wrong value with no error, so
    this must fail loudly. It is an explicit raise rather than an `assert`
    because `python -O` deletes asserts, and it is not a ValueError because
    `set_sleep` deliberately swallows those.
    """
    validate_sleep_minutes(minutes)
    seconds = minutes * 60
    if not 0 <= seconds < (1 << 16):
        raise RuntimeError(f"{seconds}s does not fit the 16-bit field")

    packet = bytearray(64)
    packet[: len(SLEEP_PACKET_COMMAND)] = SLEEP_PACKET_COMMAND
    packet[OFFSET_SLEEP_SECONDS_LSB] = seconds & 0xFF
    packet[OFFSET_SLEEP_SECONDS_MSB] = (seconds >> 8) & 0xFF
    return bytes(packet)


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


def decode_status(buf, path):
    """Turn a raw 64-byte status reply into a status dict, or None.

    Pure on purpose: no device state, no IO. That is what lets every field be
    asserted in a test without a receiver plugged in. The display hold and the
    low-battery notice stay in `query()`, because those are device state.
    """
    # A reply has to be long enough to hold the last field read below. Any
    # caller can hand this public function a shorter buffer, and a truncated
    # read is an absent reply, not a crash.
    if len(buf) <= OFFSET_SLEEP_MSB:
        return None

    if buf[1] != REPLY_MARKER:
        return None

    voltage_mv = (buf[OFFSET_VOLUME_MSB] << 8) | buf[OFFSET_VOLUME_LSB]
    return {
        "percentage": mv_to_percent(voltage_mv),
        "voltage": voltage_mv,
        "charging": buf[OFFSET_CHARGING] == CHARGING_FLAG,
        "sleep_min": ((buf[OFFSET_SLEEP_MSB] << 8) | buf[OFFSET_SLEEP_LSB]) // 60,
        "path": path,
    }


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
            fcntl.ioctl(fd, HIDIOCSFEATURE_64, QUERY_PACKET)
            time.sleep(0.04)

            buf = bytearray([0xFF] + [0x00] * 63)
            fcntl.ioctl(fd, HIDIOCGFEATURE_64, buf)

            status = decode_status(buf, path)
            if status is None:
                self._go_offline()
                return None

            status["percentage"] = self.stabilize_percentage(status["percentage"])

            self.last_status = status
            self.last_error = None

            self._update_low_battery_notice(status["percentage"], status["charging"])

            return status
        except OSError:
            self._go_offline()
            return None
        finally:
            os.close(fd)

    def set_sleep(self, minutes: int):
        """Set the automatic sleep timer (0 = never turn off)."""
        try:
            payload = build_sleep_packet(minutes)
        except ValueError:
            return False

        fd, _ = self.find_node()
        if fd is None:
            return False

        try:
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
        except (OSError, ValueError, AttributeError, RuntimeError, KeyError):
            self.noise_reduction = False

    def save_noise_reduction_state(self):
        """Persist the current value, creating the directory on first use."""
        try:
            path = noise_reduction_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump({"noise_reduction": self.noise_reduction}, handle)
            os.replace(tmp, path)
        except (OSError, ValueError, AttributeError, RuntimeError, KeyError):
            pass

    def notify(self, title: str, msg: str):
        """Send a desktop notification through notify-send, bounded in time."""
        # notify-send may be missing (FileNotFoundError) or its daemon hung
        # (TimeoutExpired, a SubprocessError): either way, skip the note
        # rather than stalling the tray.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["notify-send", title, msg, "-i", "audio-headset"],
                check=False,
                timeout=NOTIFY_TIMEOUT_SECONDS,
            )
