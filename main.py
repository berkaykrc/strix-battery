#!/usr/bin/env python3
import argparse
import json
import sys

from battery import (  # noqa: F401  - re-exported; `main` is the documented entry point
    DEFAULT_DISCHARGE_CURVE,
    DISCHARGE_CURVE_ENV_VAR,
    load_discharge_curve,
    mv_to_percent,
    parse_discharge_curve,
)
from device import (  # noqa: F401  - re-exported; `main` is the documented entry point
    ASUS_VID,
    BATTERY_DISPLAY_DEADBAND_PERCENT,
    ERROR_OFFLINE,
    ERROR_PERMISSION,
    HIDIOCGFEATURE_64,
    HIDIOCGRAWINFO,
    HIDIOCSFEATURE_64,
    LOW_BATTERY_NOTIFY_PERCENT,
    LOW_BATTERY_REARM_PERCENT,
    MAX_SLEEP_MINUTES,
    NOISE_REDUCTION_SUB_COMMAND,
    NOTIFY_TIMEOUT_SECONDS,
    QUERY_PACKET,
    STRIX_PIDS,
    StrixDevice,
    noise_reduction_state_path,
    offline_message,
    validate_sleep_minutes,
)
from icon import pil_to_qpixmap, render_tray_icon  # noqa: F401
from tray import (  # noqa: F401  - re-exported; `main` is the documented entry point
    HEADSET_ICON,
    NOISE_REDUCTION_ICON,
    SLEEP_ICON,
    _run_qt_app_with_sigint_shutdown,
    run_pyqt_tray,
    start_tray,
)


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
