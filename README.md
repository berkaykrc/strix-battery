# ASUS ROG Strix Go 2.4 Battery

[![CI](https://github.com/berkaykrc/strix-battery/actions/workflows/ci.yml/badge.svg)](https://github.com/berkaykrc/strix-battery/actions/workflows/ci.yml)

A small Linux command-line and system tray application for monitoring the battery status and configuring the automatic sleep timer of the ASUS ROG Strix Go 2.4 headset.

## Features

- Reads battery percentage, voltage, and charging status from the USB receiver.
- Provides a Qt6-based system tray application.
- Configures the automatic sleep timer from 0 to 1092 minutes.
- Provides JSON output for Waybar and Polybar.
- Sends a desktop notification when the battery reaches 15% or less.

## Requirements

- GNU/Linux and Python 3.12+
- ASUS ROG Strix Go 2.4 USB receiver
- A Qt6-compatible desktop environment with system tray support
- `notify-send` for low-battery notifications
- [`uv`](https://docs.astral.sh/uv/) is recommended

The application requires read/write access to `/dev/hidraw*`. If your user does not have access to the receiver, add the following udev rules to `/etc/udev/rules.d/70-strix-go.rules`:

```udev
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0b05", ATTRS{idProduct}=="18d6", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0b05", ATTRS{idProduct}=="18d7", TAG+="uaccess"
```

Reload and trigger the rules:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

## Installation

```bash
git clone https://github.com/berkaykrc/strix-battery.git
cd strix-battery
uv sync
```

## Usage

Run the installed command:

```bash
uv run strix-battery
uv run strix-battery --json
uv run strix-battery --set-sleep 30
uv run strix-battery --noise-reduction on
uv run strix-battery --noise-reduction off
uv run strix-battery --tray
```

`uv run` executes the command with the project's dependencies. You do not need to run `uv build` for normal local use.

You can also run the source file directly:

```bash
uv run python main.py --json
```

`--set-sleep 0` disables automatic shutdown. The maximum value is 1092 minutes because the HID packet uses a 16-bit seconds field.

### Exit codes and JSON output

The exit code is the process status, not a field in the output. A panel script
reads `$?` after the command finishes. It is the same in text and `--json` mode:

| `$?` | Meaning |
| ---- | ------- |
| 0    | Reported successfully |
| 1    | Headset offline / out of range (also what an offline `--json` reports) |
| 2    | Bad usage, e.g. a `--set-sleep` value out of range |

`--json` prints exactly one line to stdout. Online:

```json
{"status": "online", "percentage": 71, "voltage": 3904, "charging": false, "sleep_min": 0, "text": "%71"}
```

Offline — there is no `percentage` key, so a panel can key on `status`:

```json
{"status": "offline", "text": "Headset offline"}
```

Example for a Waybar/Polybar custom module:

```bash
out=$(strix-battery --json); code=$?
[ "$code" -eq 0 ] || out='{"text": "---"}'
echo "$out"
```

### Battery percentage

The headset reports its battery as a millivolt value. The displayed percentage
follows Armoury Crate's quarter steps (100 / 75 / 50 / 25 / 10), so the
number matches what Armoury Crate shows. The `STRIX_BATTERY_CURVE` override
still applies to the underlying millivolt-to-percentage helper used by tests
and tooling:

```bash
export STRIX_BATTERY_CURVE="3300:0,3600:25,4000:80,4150:100"
```

Take readings with the headset powered on and connected, since a cell at rest
reads higher than the same cell under load. Voltage must ascend and percentage
must never fall. A malformed value is reported once on stderr and then ignored,
so a typo cannot stop the tray from rendering.

## Development

```bash
uv sync --group dev
uv run python -m unittest discover -s tests
uv run --group dev ruff check .
uv run --group dev ruff format --check .
uv run --group dev ty check
```

All four must pass.

## Building Distribution Packages

Build wheel and source archive files for PyPI or GitHub Releases:

```bash
uv build
```

The packages are written to `dist/`. This command does not start the application; it only creates distributable files.

Hardware-dependent paths cannot be run without a real Strix Go 2.4 receiver. Conversion and validation tests run without hardware.

## Limitations

- Linux `hidraw` and the ASUS ROG Strix Go 2.4 device IDs are required.
- Other ROG models and USB receivers are not supported.
- System tray behavior may vary by desktop environment.
