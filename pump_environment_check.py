"""
Quick environment and hardware readiness check for NE-1000 control work.

Usage:
    python pump_environment_check.py
"""

from __future__ import annotations

import sys
from typing import Iterable

import serial.tools.list_ports
from nesp_lib import Port


def list_com_ports() -> Iterable[str]:
    """Return human-readable COM port descriptions."""
    ports = serial.tools.list_ports.comports()
    for p in ports:
        yield f"{p.device} - {p.description}"


def main() -> int:
    print("=== New Era Pump Environment Check ===")
    print(f"Python: {sys.version.split()[0]}")

    port_lines = list(list_com_ports())
    if not port_lines:
        print("No COM ports detected right now.")
        return 0

    print("Detected COM ports:")
    for line in port_lines:
        print(f"  - {line}")

    # Optional connectivity probe on the first detected port.
    first_port = port_lines[0].split(" - ", 1)[0]
    print(f"\nTrying a basic Port open/close on {first_port} ...")
    try:
        with Port(first_port):
            print("Port open/close test: OK")
    except Exception as exc:  # pragma: no cover - hardware-dependent
        print(f"Port open/close test failed: {exc}")
        print("This may be normal if no NE-1000 is connected to that port.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
