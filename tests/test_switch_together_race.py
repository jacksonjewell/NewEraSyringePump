"""Regression test for the Switch-Together race-condition overdose bug.

PumpControllerApp._run_group_action is invoked from "Apply"/"Run"/"Stop" on
a pump panel when "Switch Together" is enabled in Dual or Triple mode. It
must propagate the source panel's settings (rate, volume, syringe, etc.)
to every target panel before sending those settings to the physical pump
on the worker thread.

The previous implementation used ``panel.after(0, lambda: p.apply_settings_from_snapshot(snapshot))``
to copy the snapshot into the target panel — but ``after(0, ...)`` only
*schedules* a callback on the GUI thread; it does not block. The worker
thread immediately called ``panel.apply_settings_sync()`` which reads the
panel's Tk variables (rate, volume, syringe diameter) and pushes them to
the connected pump. Because the apply_from_snapshot callback hadn't run
yet, ``apply_settings_sync`` saw the panel's previous, stale values and
programmed the target pump with them. In Dual/Triple "Switch Together"
mode that meant any non-source pump was driven at its previously
configured rate/volume — a serious overdose risk.

This test verifies that ``_run_group_action`` propagates the source
panel's settings to the target panel BEFORE programming the pump, so the
target pump receives the source's rate/volume rather than the target's
stale ones.

The test runs without instantiating Tk widgets or real serial hardware so
it can run on a headless CI box.
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
    tk_mod.Listbox = _Widget
    tk_mod.Text = _Widget
    tk_mod.Widget = _Widget
    tk_mod.Event = object
    tk_mod.END = "end"
    tk_mod.TclError = _TclError
    sys.modules["tkinter"] = tk_mod

    ttk_mod = types.ModuleType("tkinter.ttk")
    for name in ("Frame", "LabelFrame", "Label", "Button", "Entry", "Combobox",
                 "Checkbutton", "Scrollbar", "Style", "Progressbar"):
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

    serial_mod.Serial = _StubSerial
    tools_mod = types.ModuleType("serial.tools")
    list_ports_mod = types.ModuleType("serial.tools.list_ports")
    list_ports_mod.comports = lambda: []
    tools_mod.list_ports = list_ports_mod
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


class _Var:
    def __init__(self, value=""):
        self._v = value
    def get(self):
        return self._v
    def set(self, value):
        self._v = str(value)


class _FakePump:
    def __init__(self) -> None:
        self.syringe_diameter: float = 0.0
        self.pumping_rate: float = 0.0
        self.pumping_direction: str = ""
        self.pumping_volume: float = 0.0
        self.was_run: bool = False
        self.was_stopped: bool = False


class _FakePanel:
    """Stand-in for PumpPanel using only the methods we need under test."""

    snapshot_settings = pcg.PumpPanel.snapshot_settings
    apply_settings_from_snapshot = pcg.PumpPanel.apply_settings_from_snapshot
    apply_settings_sync = pcg.PumpPanel.apply_settings_sync
    _on_syringe_preset_change = pcg.PumpPanel._on_syringe_preset_change

    def __init__(self, pump_index: int) -> None:
        self.pump_index = pump_index
        self.syringe_var = _Var("BD 10 mL (10 cc)")
        self.custom_diameter_var = _Var("14.50")
        self.rate_units_var = _Var("mL/min")
        self.rate_var = _Var("0.5")
        self.dispense_mode_var = _Var("Volume")
        self.volume_ul_var = _Var("0")
        self._vol_disp_unit_var = _Var("uL")
        self.direction_var = _Var("Infuse")
        self.connection = types.SimpleNamespace(pump=_FakePump())
        # _after_calls records whether after() was called against this panel,
        # but we never actually run those callbacks unless asked.
        self.after_calls = []

    def _require_pump(self) -> _FakePump:
        return self.connection.pump

    def run_sync(self) -> None:
        self.connection.pump.was_run = True

    def stop_sync(self) -> None:
        self.connection.pump.was_stopped = True

    def after(self, _delay, fn=None, *a, **kw):
        # Do NOT invoke fn — we're modelling the real Tk after() which
        # *schedules* the callback on the GUI thread. Tests that need the
        # callback to run will pump it manually via _drain_app_after().
        self.after_calls.append(fn)


class _FakeApp:
    """Stand-in for PumpControllerApp for the methods exercised here."""

    _run_group_action = pcg.PumpControllerApp._run_group_action
    _guarded_work = staticmethod(pcg.PumpControllerApp._guarded_work)
    _targets_for_action = pcg.PumpControllerApp._targets_for_action
    _mode_target_indices = pcg.PumpControllerApp._mode_target_indices
    apply_with_mode = pcg.PumpControllerApp.apply_with_mode
    run_with_mode = pcg.PumpControllerApp.run_with_mode
    stop_with_mode = pcg.PumpControllerApp.stop_with_mode

    def __init__(self) -> None:
        self.panels = [_FakePanel(1), _FakePanel(2), _FakePanel(3)]
        self.mode_var = _Var("Dual mode")
        self.switch_together_var = _Var(True)
        # Single-threaded scheduler queue mimicking Tk's main loop.
        self._after_queue: list = []
        self._after_lock = threading.Lock()
        self._after_thread = threading.Thread(target=self._after_loop, daemon=True)
        self._after_thread_running = True
        self._after_thread.start()

    def after(self, _delay, fn=None, *a, **kw):
        # Mimic Tk after(): enqueue the callback, return promptly.
        if fn is None:
            return None
        with self._after_lock:
            self._after_queue.append((fn, a, kw))
        return None

    def _after_loop(self) -> None:
        # Continuously drain the queue on a separate thread, with a small
        # delay to faithfully model the GUI thread being slower than the
        # worker thread that schedules the callbacks.
        while self._after_thread_running:
            cb = None
            with self._after_lock:
                if self._after_queue:
                    cb = self._after_queue.pop(0)
            if cb is None:
                time.sleep(0.001)
                continue
            fn, a, kw = cb
            # Simulate GUI thread latency that the bug relies on.
            time.sleep(0.05)
            try:
                fn(*a, **kw)
            except Exception:
                pass

    def stop(self) -> None:
        self._after_thread_running = False
        self._after_thread.join(timeout=2.0)


class SwitchTogetherRace(unittest.TestCase):
    def test_target_pump_receives_source_settings_not_stale_values(self) -> None:
        """In Dual mode + Switch Together, pressing Run on pump 1 must program
        pump 2 with pump 1's rate/volume — not pump 2's previous values."""
        app = _FakeApp()
        try:
            src = app.panels[0]
            tgt = app.panels[1]

            # Pump 2 has stale "dangerous" settings from a prior session:
            # 10 mL/min, 5000 µL volume.
            tgt.rate_var.set("10")
            tgt.volume_ul_var.set("5000")
            tgt._vol_disp_unit_var.set("uL")

            # User now wants a small dose on pump 1: 0.1 mL/min, 100 µL.
            src.rate_var.set("0.1")
            src.volume_ul_var.set("100")
            src._vol_disp_unit_var.set("uL")
            src.dispense_mode_var.set("Volume")

            # Trigger Run with mode = Dual + Switch Together. This spawns a
            # worker thread that should propagate src settings to tgt before
            # programming tgt's pump.
            app.run_with_mode(src)

            # Wait for the worker thread + GUI scheduler to settle.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if tgt.connection.pump.was_run and src.connection.pump.was_run:
                    break
                time.sleep(0.01)
            # Give the after-queue a moment to drain status updates.
            time.sleep(0.2)

            self.assertTrue(src.connection.pump.was_run, "source pump never ran")
            self.assertTrue(tgt.connection.pump.was_run, "target pump never ran")

            # The critical assertions: target pump must NOT have been
            # programmed with its stale values. With the race-condition bug,
            # pumping_rate would be 10.0 (mL/min) and pumping_volume would
            # be 5.0 (mL == 5000 µL).
            self.assertAlmostEqual(
                tgt.connection.pump.pumping_rate, 0.1, places=6,
                msg="target pump rate wasn't synced from source — Switch Together race regression",
            )
            self.assertAlmostEqual(
                tgt.connection.pump.pumping_volume, 0.1, places=6,
                msg="target pump volume wasn't synced from source — Switch Together race regression",
            )
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main()
