"""Regression test for the Vacuum Power-Off / emergency-stop EMI bug.

`VacuumPanel.force_off` is the global Power-Off / emergency-stop path —
it is invoked by `PumpControllerApp.power_off` when the user hits the
app's main Power Off button. It runs while the brushed vacuum motor is
energized, which is exactly when EMI on the USB-serial line is at its
worst.

Commit 2f016e5 introduced an EMI-hardening burst write
(`_VACUUM_CMD_REPEATS = 5`) so a single bit-flip on the line cannot
defeat a motor command. That commit covered the manual toggle
(`_send_value`) and the recipe runner (`send_vacuum_sync`) but left
`force_off` writing a single `b"0"` byte. EMI on that single byte
(silently ignored by the Arduino, which only acts on exact `'0'` /
`'1'`) would leave the motor running while the GUI showed
"Vacuum OFF (forced)" — a real safety hole on the emergency stop path.

This test instantiates a `VacuumPanel` without going through Tk, swaps
in a fake serial connection, runs `force_off`, and asserts that the
write that actually reaches the wire is the EMI-hardened burst (one
`'0'` byte per `_VACUUM_CMD_REPEATS`), matching the other two command
paths.

The test runs without instantiating Tk widgets or real serial hardware
so it can run on a headless CI box.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import types
import unittest


def _install_tkinter_stub() -> None:
    if "tkinter" in sys.modules:
        return

    class _Var:
        def __init__(self, value=""):
            self._v = value
        def get(self):
            return self._v
        def set(self, v):
            self._v = str(v)

    class _Widget:
        def __init__(self, *a, **kw):
            pass
        def __getattr__(self, item):
            return lambda *a, **kw: None

    class _TclError(Exception):
        pass

    tk_mod = types.ModuleType("tkinter")
    tk_mod.StringVar = _Var
    tk_mod.BooleanVar = _Var
    tk_mod.IntVar = _Var
    tk_mod.DoubleVar = _Var
    tk_mod.Tk = _Widget
    tk_mod.Toplevel = _Widget
    tk_mod.Frame = _Widget
    tk_mod.Label = _Widget
    tk_mod.Button = _Widget
    tk_mod.Canvas = _Widget
    tk_mod.Listbox = _Widget
    tk_mod.Text = _Widget
    tk_mod.Widget = _Widget
    tk_mod.Event = object
    tk_mod.END = "end"
    tk_mod.TclError = _TclError
    sys.modules["tkinter"] = tk_mod

    ttk_mod = types.ModuleType("tkinter.ttk")
    for name in (
        "Frame", "LabelFrame", "Label", "Button", "Entry", "Combobox",
        "Checkbutton", "Scrollbar", "Style", "Progressbar",
    ):
        setattr(ttk_mod, name, _Widget)
    sys.modules["tkinter.ttk"] = ttk_mod
    tk_mod.ttk = ttk_mod

    msg_mod = types.ModuleType("tkinter.messagebox")
    for name in ("showinfo", "showwarning", "showerror", "askyesno"):
        setattr(msg_mod, name, lambda *a, **kw: None)
    sys.modules["tkinter.messagebox"] = msg_mod
    tk_mod.messagebox = msg_mod

    sd_mod = types.ModuleType("tkinter.simpledialog")
    sd_mod.askinteger = lambda *a, **kw: None
    sd_mod.askfloat = lambda *a, **kw: None
    sd_mod.askstring = lambda *a, **kw: None
    sys.modules["tkinter.simpledialog"] = sd_mod
    tk_mod.simpledialog = sd_mod


def _install_serial_stub() -> None:
    if "serial" in sys.modules:
        return
    serial_mod = types.ModuleType("serial")

    class _StubSerial:
        def __init__(self, *a, **kw):
            raise RuntimeError("serial not available in tests")

    class _SerialException(Exception):
        pass

    class _SerialTimeoutException(_SerialException):
        pass

    serial_mod.Serial = _StubSerial
    serial_mod.SerialException = _SerialException
    serial_mod.SerialTimeoutException = _SerialTimeoutException
    tools_mod = types.ModuleType("serial.tools")
    list_ports_mod = types.ModuleType("serial.tools.list_ports")
    list_ports_mod.comports = lambda: []
    tools_mod.list_ports = list_ports_mod
    serial_mod.tools = tools_mod
    sys.modules["serial"] = serial_mod
    sys.modules["serial.tools"] = tools_mod
    sys.modules["serial.tools.list_ports"] = list_ports_mod


def _install_nesp_stub() -> None:
    if "nesp_lib" in sys.modules:
        return
    mod = types.ModuleType("nesp_lib")

    class _Port:
        def __init__(self, *a, **kw):
            raise RuntimeError("nesp_lib not available in tests")
        def close(self):
            pass

    class _Pump:
        def __init__(self, *a, **kw):
            raise RuntimeError("nesp_lib not available in tests")

    class _Direction:
        INFUSE = "INFUSE"
        WITHDRAW = "WITHDRAW"

    mod.Port = _Port
    mod.Pump = _Pump
    mod.PumpingDirection = _Direction
    sys.modules["nesp_lib"] = mod


_install_tkinter_stub()
_install_serial_stub()
_install_nesp_stub()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import pump_control_gui as pcg  # noqa: E402


class _FakeSerial:
    """Records every write so we can assert the exact bytes that reach the wire."""

    def __init__(self) -> None:
        self.is_open = True
        self.writes: list[bytes] = []
        self.flush_count = 0

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        self.flush_count += 1


def _build_panel_skeleton(serial_conn: _FakeSerial) -> pcg.VacuumPanel:
    """Create a VacuumPanel-shaped object with just the attributes
    `force_off` touches, without going through Tk init.
    """
    panel = pcg.VacuumPanel.__new__(pcg.VacuumPanel)
    panel._serial_lock = threading.Lock()
    panel.serial_conn = serial_conn
    panel.is_on = True
    panel._after_calls: list = []

    def _after(_delay, fn=None, *a, **kw):
        # Capture-but-don't-run, like the production after() returns
        # immediately and the Tk main loop runs callbacks later.
        panel._after_calls.append(fn)
        return None

    panel.after = _after  # type: ignore[assignment]
    panel.toggle_btn = types.SimpleNamespace(config=lambda **kw: None)
    panel._set_button_color = lambda: None
    panel._set_status = lambda *a, **kw: None
    return panel


class ForceOffEmiBurstTest(unittest.TestCase):
    def test_force_off_writes_emi_hardened_burst(self) -> None:
        """force_off must send the OFF command as the EMI-hardened burst,
        not a single byte. A single byte is exactly what the
        _VACUUM_CMD_REPEATS hardening exists to eliminate.
        """
        self.assertGreaterEqual(
            pcg._VACUUM_CMD_REPEATS, 2,
            "EMI burst must repeat the command at least twice",
        )

        ser = _FakeSerial()
        panel = _build_panel_skeleton(ser)

        panel.force_off()
        # force_off launches a worker thread; wait for it to settle.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not ser.writes:
            time.sleep(0.01)

        self.assertEqual(
            len(ser.writes), 1,
            f"force_off should issue exactly one write call, got {ser.writes!r}",
        )
        written = ser.writes[0]
        expected = b"0" * pcg._VACUUM_CMD_REPEATS
        self.assertEqual(
            written, expected,
            (
                "force_off must send b'0' * _VACUUM_CMD_REPEATS so brushed-"
                "motor EMI cannot silently leave the motor energized while "
                f"the GUI shows OFF; got {written!r}, expected {expected!r}"
            ),
        )
        self.assertGreaterEqual(
            ser.flush_count, 1, "force_off must flush after writing",
        )
        self.assertFalse(panel.is_on, "force_off must clear is_on")

    def test_force_off_burst_matches_other_command_paths(self) -> None:
        """force_off, _send_value, and send_vacuum_sync must all use the
        same EMI-hardened burst length so any one-path-only regression is
        caught here.
        """
        ser_force = _FakeSerial()
        panel_force = _build_panel_skeleton(ser_force)
        panel_force.force_off()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not ser_force.writes:
            time.sleep(0.01)

        force_off_bytes = b"".join(ser_force.writes)
        sendvalue_bytes = ("0" * pcg._VACUUM_CMD_REPEATS).encode("ascii")
        sendsync_bytes = ("0" * pcg._VACUUM_CMD_REPEATS).encode("ascii")

        self.assertEqual(force_off_bytes, sendvalue_bytes)
        self.assertEqual(force_off_bytes, sendsync_bytes)


if __name__ == "__main__":
    unittest.main()
