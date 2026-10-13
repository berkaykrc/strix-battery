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

### Battery curve

The shipped millivolt-to-percentage curve is the linear map and is deliberately
uncalibrated. If you have measured your own cell, override it with ascending
`mV:percent` pairs:

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
