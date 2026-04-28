"""Regression test for the volume-unit round-trip bug.

PumpPanel.snapshot_settings() must always emit "volume_ul" in true
microliters, regardless of which display unit (uL / mL / L) the user has
selected, and apply_settings_from_snapshot() must reset the panel's
display unit to "uL" so that apply_settings_sync() reads the value back as
microliters. Otherwise a recipe round-trip can silently dispense 1000×
or 1,000,000× the intended volume.

This module exercises that contract without instantiating Tk widgets so
it can run on a headless CI box.
"""

from __future__ import annotations

import os
import sys
import types
import unittest


# Provide minimal stubs for hardware deps so importing the GUI module does
# not fail in an environment without serial/tk hardware. Only the pieces
# the GUI module touches at import time are stubbed.
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
    tk_mod.StringVar = _Var  # type: ignore[attr-defined]
    tk_mod.BooleanVar = _Var  # type: ignore[attr-defined]
    tk_mod.IntVar = _Var  # type: ignore[attr-defined]
    tk_mod.DoubleVar = _Var  # type: ignore[attr-defined]
    tk_mod.Tk = _Widget  # type: ignore[attr-defined]
    tk_mod.Toplevel = _Widget  # type: ignore[attr-defined]
    tk_mod.Frame = _Widget  # type: ignore[attr-defined]
    tk_mod.Label = _Widget  # type: ignore[attr-defined]
    tk_mod.Button = _Widget  # type: ignore[attr-defined]
    tk_mod.Listbox = _Widget  # type: ignore[attr-defined]
    tk_mod.Text = _Widget  # type: ignore[attr-defined]
    tk_mod.Widget = _Widget  # type: ignore[attr-defined]
    tk_mod.Event = object  # type: ignore[attr-defined]
    tk_mod.END = "end"  # type: ignore[attr-defined]
    tk_mod.TclError = _TclError  # type: ignore[attr-defined]
    sys.modules["tkinter"] = tk_mod

    ttk_mod = types.ModuleType("tkinter.ttk")
    for name in ("Frame", "LabelFrame", "Label", "Button", "Entry", "Combobox",
                 "Checkbutton", "Scrollbar", "Style", "Progressbar"):
        setattr(ttk_mod, name, _Widget)
    sys.modules["tkinter.ttk"] = ttk_mod
    tk_mod.ttk = ttk_mod  # type: ignore[attr-defined]

    msg_mod = types.ModuleType("tkinter.messagebox")
    for name in ("showinfo", "showwarning", "showerror", "askyesno"):
        setattr(msg_mod, name, lambda *a, **kw: None)
    sys.modules["tkinter.messagebox"] = msg_mod
    tk_mod.messagebox = msg_mod  # type: ignore[attr-defined]

    sd_mod = types.ModuleType("tkinter.simpledialog")
    sd_mod.askinteger = lambda *a, **kw: None  # type: ignore[attr-defined]
    sd_mod.askfloat = lambda *a, **kw: None  # type: ignore[attr-defined]
    sd_mod.askstring = lambda *a, **kw: None  # type: ignore[attr-defined]
    sys.modules["tkinter.simpledialog"] = sd_mod
    tk_mod.simpledialog = sd_mod  # type: ignore[attr-defined]


def _install_serial_stub() -> None:
    if "serial" in sys.modules:
        return
    serial_mod = types.ModuleType("serial")

    class _StubSerial:
        def __init__(self, *a, **kw):
            raise RuntimeError("serial not available in tests")

    serial_mod.Serial = _StubSerial  # type: ignore[attr-defined]
    tools_mod = types.ModuleType("serial.tools")
    list_ports_mod = types.ModuleType("serial.tools.list_ports")
    list_ports_mod.comports = lambda: []  # type: ignore[attr-defined]
    tools_mod.list_ports = list_ports_mod  # type: ignore[attr-defined]
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

    mod.Port = _Port  # type: ignore[attr-defined]
    mod.Pump = _Pump  # type: ignore[attr-defined]
    mod.PumpingDirection = _Direction  # type: ignore[attr-defined]
    sys.modules["nesp_lib"] = mod


_install_tkinter_stub()
_install_serial_stub()
_install_nesp_stub()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import pump_control_gui as pcg  # noqa: E402


class _Var:
    """Tiny stand-in for tk.StringVar."""

    def __init__(self, value: str = ""):
        self._v = value

    def get(self) -> str:
        return self._v

    def set(self, value: str) -> None:
        self._v = str(value)


class _FakePump:
    def __init__(self) -> None:
        self.syringe_diameter: float = 0.0
        self.pumping_rate: float = 0.0
        self.pumping_direction: str = ""
        self.pumping_volume: float = 0.0


class _FakePanel:
    """Bare PumpPanel that re-uses the real snapshot/apply methods."""

    snapshot_settings = pcg.PumpPanel.snapshot_settings
    apply_settings_from_snapshot = pcg.PumpPanel.apply_settings_from_snapshot
    apply_settings_sync = pcg.PumpPanel.apply_settings_sync
    _on_syringe_preset_change = pcg.PumpPanel._on_syringe_preset_change

    def __init__(self) -> None:
        self.syringe_var = _Var("BD 10 mL (10 cc)")
        self.custom_diameter_var = _Var("14.50")
        self.rate_units_var = _Var("mL/min")
        self.rate_var = _Var("0.5")
        self.dispense_mode_var = _Var("Volume")
        self.volume_ul_var = _Var("0")
        self._vol_disp_unit_var = _Var("uL")
        self.direction_var = _Var("Infuse")
        self.connection = types.SimpleNamespace(pump=_FakePump())

    def _require_pump(self) -> _FakePump:
        return self.connection.pump


class VolumeUnitRoundTrip(unittest.TestCase):
    def test_snapshot_in_microliters_when_unit_is_uL(self) -> None:
        p = _FakePanel()
        p.volume_ul_var.set("250")
        p._vol_disp_unit_var.set("uL")
        snap = p.snapshot_settings()
        self.assertEqual(snap["volume_ul"], "250")

    def test_snapshot_converts_mL_to_microliters(self) -> None:
        p = _FakePanel()
        p.volume_ul_var.set("5")
        p._vol_disp_unit_var.set("mL")
        snap = p.snapshot_settings()
        self.assertEqual(float(snap["volume_ul"]), 5000.0)

    def test_snapshot_converts_L_to_microliters(self) -> None:
        p = _FakePanel()
        p.volume_ul_var.set("0.001")
        p._vol_disp_unit_var.set("L")
        snap = p.snapshot_settings()
        self.assertAlmostEqual(float(snap["volume_ul"]), 1000.0, places=4)

    def test_apply_resets_unit_picker_to_uL(self) -> None:
        p = _FakePanel()
        p._vol_disp_unit_var.set("mL")
        p.apply_settings_from_snapshot({
            "syringe": "BD 10 mL (10 cc)",
            "custom_diameter_mm": "14.50",
            "rate_units": "mL/min",
            "rate_value": "0.5",
            "dispense_mode": "Volume",
            "volume_ul": "200",
            "direction": "Infuse",
        })
        self.assertEqual(p._vol_disp_unit_var.get(), "uL")
        self.assertEqual(p.volume_ul_var.get(), "200")

    def test_round_trip_through_apply_sync_in_mL_display(self) -> None:
        """Save while showing mL, then load+apply -> pump volume must equal original mL."""
        src = _FakePanel()
        src.volume_ul_var.set("5")
        src._vol_disp_unit_var.set("mL")
        snap = src.snapshot_settings()

        dst = _FakePanel()
        dst._vol_disp_unit_var.set("mL")
        dst.apply_settings_from_snapshot(snap)
        dst.apply_settings_sync()

        # Pump volume is in mL: 5 mL was entered, expect 5 mL on the pump.
        self.assertAlmostEqual(dst.connection.pump.pumping_volume, 5.0, places=6)

    def test_round_trip_uL_with_mL_display_preset(self) -> None:
        """Snapshot taken in uL must apply correctly even if dst shows mL."""
        src = _FakePanel()
        src.volume_ul_var.set("100")
        src._vol_disp_unit_var.set("uL")
        snap = src.snapshot_settings()

        dst = _FakePanel()
        dst._vol_disp_unit_var.set("mL")  # would have caused a 1000x overdose
        dst.apply_settings_from_snapshot(snap)
        dst.apply_settings_sync()

        # 100 µL == 0.1 mL on the pump.
        self.assertAlmostEqual(dst.connection.pump.pumping_volume, 0.1, places=6)


if __name__ == "__main__":
    unittest.main()
