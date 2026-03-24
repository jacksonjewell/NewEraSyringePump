"""
Tkinter GUI for controlling three New Era NE-1000 syringe pumps and an Arduino vacuum.
"""

from __future__ import annotations

import copy
import json
import threading
import time
import tkinter as tk
import uuid
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable, Optional

import serial
import serial.tools.list_ports
from nesp_lib import Port, Pump, PumpingDirection


DARK = {
    "bg": "#1E1E2E",
    "panel": "#2A2A3C",
    "input": "#353548",
    "fg": "#E8E8F0",
    "dim": "#9090A8",
    "btn": "#3A3A50",
    "btn_hover": "#2E2E42",
    "btn_border": "#50506A",
}
LIGHT = {
    "bg": "SystemButtonFace",
    "panel": "SystemButtonFace",
    "input": "white",
    "fg": "black",
    "dim": "#555555",
    "btn": "#E0E0E0",
    "btn_hover": "#C8C8C8",
    "btn_border": "#AAAAAA",
}


def apply_theme(style: ttk.Style, theme: dict) -> None:
    style.configure(".", background=theme["bg"], foreground=theme["fg"],
                     fieldbackground=theme["input"], borderwidth=0)
    style.configure("TFrame", background=theme["bg"])
    style.configure("TLabel", background=theme["bg"], foreground=theme["fg"])
    style.configure("TLabelFrame", background=theme["bg"], foreground=theme["fg"])
    style.configure("TLabelFrame.Label", background=theme["bg"], foreground=theme["fg"])
    style.configure("TEntry", fieldbackground=theme["input"], foreground=theme["fg"],
                     insertcolor=theme["fg"])
    style.configure("TCombobox", fieldbackground=theme["input"], foreground=theme["fg"],
                     arrowcolor=theme["fg"])
    style.map("TCombobox",
              fieldbackground=[("readonly", theme["input"])],
              foreground=[("readonly", theme["fg"])],
              selectbackground=[("readonly", theme["input"])],
              selectforeground=[("readonly", theme["fg"])])
    style.configure("TButton", background=theme["btn"], foreground=theme["fg"],
                     borderwidth=1, relief="solid", padding=(10, 6),
                     bordercolor=theme["btn_border"],
                     lightcolor=theme["btn_border"],
                     darkcolor=theme["btn_border"])
    style.map("TButton", background=[("active", theme["btn_hover"]),
                                      ("pressed", theme["btn_hover"])])
    style.configure("TCheckbutton", background=theme["bg"], foreground=theme["fg"])


SYRINGE_PRESETS_MM = {
    "BD 1 mL": 4.78,
    "BD 3 mL": 8.66,
    "BD 5 mL (5 cc)": 12.06,
    "BD 10 mL (10 cc)": 14.50,
    "BD 20 mL (20 cc)": 19.13,
    "BD 30 mL (30 cc)": 21.70,
    "BD 50 mL (50 cc)": 26.70,
    "BD 60 mL (60 cc)": 26.72,
    "Custom": None,
}

RATE_UNITS = ["mL/min", "mL/hr", "uL/min", "uL/hr"]
DISPENSE_MODES = ["Continuous", "Volume"]
BAUD_RATES = ["9600", "19200"]
VACUUM_BAUD_RATES = ["9600", "115200", "57600", "38400"]
STX = 0x02
ETX = 0x03

RECIPES_JSON_VERSION = 1


def recipes_file_path() -> Path:
    return Path(__file__).resolve().parent / "recipes.json"


def load_stored_recipes() -> list[dict[str, Any]]:
    path = recipes_file_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("recipes"), list):
        return raw["recipes"]
    return []


def save_stored_recipes(recipes: list[dict[str, Any]]) -> None:
    path = recipes_file_path()
    payload = {"version": RECIPES_JSON_VERSION, "recipes": recipes}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def pump_labels_file_path() -> Path:
    return Path(__file__).resolve().parent / "pump_labels.json"


def load_pump_labels_file() -> dict[str, str]:
    path = pump_labels_file_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    for k in ("1", "2", "3"):
        if k in data and data[k] is not None:
            out[k] = str(data[k]).strip()
    return out


def save_pump_labels_file(labels: dict[str, str]) -> None:
    payload = {str(k): (labels.get(str(k), "") or "").strip() for k in (1, 2, 3)}
    pump_labels_file_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")


def run_on_main_thread(app: tk.Tk, fn: Callable[[], Any], timeout: float = 120.0) -> Any:
    """Run ``fn`` on the Tk main thread and return its result (or raise)."""
    result: list[Any] = []
    err: list[BaseException] = []
    event = threading.Event()

    def wrapper() -> None:
        try:
            result.append(fn())
        except BaseException as exc:
            err.append(exc)
        finally:
            event.set()

    app.after(0, wrapper)
    if not event.wait(timeout=timeout):
        raise TimeoutError("GUI thread timed out")
    if err:
        raise err[0]
    return result[0] if result else None


def abortable_sleep(seconds: float, abort_event: threading.Event, chunk: float = 0.15) -> None:
    """Sleep for ``seconds`` but return early if ``abort_event`` becomes set."""
    total = max(0.0, float(seconds))
    end = time.monotonic() + total
    while time.monotonic() < end:
        if abort_event.is_set():
            return
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(chunk, remaining))


def recipe_display_name(recipe: dict[str, Any]) -> str:
    name = recipe.get("name", "(untitled)")
    steps = recipe.get("steps")
    if isinstance(steps, list) and len(steps) > 0:
        return f"{name}  [sequence]"
    return str(name)


def _pump_step_name(pnum: Any, nicknames: Optional[dict[int, str]]) -> str:
    try:
        n = int(pnum)
    except (TypeError, ValueError):
        return f"pump {pnum}"
    nick = (nicknames or {}).get(n, "").strip()
    if nick:
        return f"pump {n} ({nick})"
    return f"pump {n}"


def format_step_summary(step: dict[str, Any], nicknames: Optional[dict[int, str]] = None) -> str:
    t = step.get("type", "?")
    if t == "delay":
        return f"Delay {step.get('seconds', '?')} s"
    if t == "connect_pump":
        pn = _pump_step_name(step.get("pump"), nicknames)
        return (
            f"Connect {pn} → {step.get('com')} "
            f"baud {step.get('baud')} addr {step.get('address')}"
        )
    if t == "disconnect_pump":
        return f"Disconnect {_pump_step_name(step.get('pump'), nicknames)}"
    if t == "apply_pump":
        s = step.get("settings") if isinstance(step.get("settings"), dict) else {}
        sy = s.get("syringe", "")
        rv = s.get("rate_value", "")
        ru = s.get("rate_units", "")
        dm = s.get("dispense_mode", "")
        return f"Apply {_pump_step_name(step.get('pump'), nicknames)} — {sy}, {rv} {ru}, {dm}"
    if t == "run_pump":
        return f"Run {_pump_step_name(step.get('pump'), nicknames)}"
    if t == "stop_pump":
        return f"Stop {_pump_step_name(step.get('pump'), nicknames)}"
    if t == "vacuum_connect":
        b = step.get("baud", 9600)
        return f"Vacuum connect {step.get('com')} @ {b}"
    if t == "vacuum_disconnect":
        return "Vacuum disconnect"
    if t == "vacuum_on":
        return "Vacuum ON (send 1)"
    if t == "vacuum_off":
        return "Vacuum OFF (send 0)"
    return str(step)


def format_step_list_line(index: int, step: dict[str, Any], nicknames: Optional[dict[int, str]] = None) -> str:
    body = format_step_summary(step, nicknames)
    lab = (step.get("label") or step.get("step_label") or "").strip()
    if lab:
        return f"{index + 1}. [{lab}] {body}"
    return f"{index + 1}. {body}"


def detected_port_names() -> list[str]:
    ports = serial.tools.list_ports.comports()
    return sorted([p.device for p in ports])


def probe_pump_address(com_name: str, address: int, baud_rate: int, timeout: float = 1.5) -> bool:
    """Send a bare status query to the pump and check for a valid STX...ETX reply."""
    conn = None
    try:
        conn = serial.Serial(port=com_name, baudrate=baud_rate, timeout=timeout)
        conn.reset_input_buffer()
        request = f"{address}\r".encode()
        conn.write(request)
        conn.flush()
        first = conn.read(1)
        if not first or first[0] != STX:
            return False
        buf = bytearray()
        while True:
            chunk = conn.read(1)
            if not chunk:
                return False
            buf.extend(chunk)
            if buf[-1] == ETX:
                return True
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def to_ml_per_min(value: float, units: str) -> float:
    if units == "mL/min":
        return value
    if units == "mL/hr":
        return value / 60.0
    if units == "uL/min":
        return value / 1000.0
    if units == "uL/hr":
        return value / 60000.0
    raise ValueError(f"Unknown rate units: {units}")


@dataclass
class PumpConnection:
    port: Optional[Port] = None
    pump: Optional[Pump] = None

    def close(self) -> None:
        if self.port is not None:
            self.port.close()
        self.port = None
        self.pump = None


class PumpPanel(ttk.LabelFrame):
    def __init__(self, master: tk.Widget, app: "PumpControllerApp", pump_index: int, ports: list[str]) -> None:
        super().__init__(master, text=f"Pump {pump_index}", padding=10)
        self.app = app
        self.pump_index = pump_index
        self.nickname_var = tk.StringVar(value="")
        self.connection = PumpConnection()
        self.run_start_ts: Optional[float] = None
        self._polling = False
        self._build(ports)

    def _refresh_frame_title(self) -> None:
        nick = self.nickname_var.get().strip()
        if nick:
            self.configure(text=f"Pump {self.pump_index} — {nick}")
        else:
            self.configure(text=f"Pump {self.pump_index}")

    def _on_nickname_changed(self, *_a: Any) -> None:
        self._refresh_frame_title()
        self.app.schedule_save_pump_labels()

    def _build(self, ports: list[str]) -> None:
        ttk.Label(self, text="Display name").grid(row=0, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.nickname_var, width=36).grid(
            row=0, column=1, columnspan=5, sticky="w", padx=(6, 0)
        )

        ttk.Label(self, text="COM Port").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.com_var = tk.StringVar(value=(ports[0] if ports else ""))
        self.com_combo = ttk.Combobox(self, textvariable=self.com_var, values=ports, width=12)
        self.com_combo.grid(row=1, column=1, sticky="w", padx=(6, 10), pady=(6, 0))

        ttk.Label(self, text="Address").grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.address_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.address_var, width=6).grid(row=1, column=3, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self, text="Baud Rate").grid(row=1, column=4, sticky="w", padx=(10, 0), pady=(6, 0))
        # NE-1000 pumps often ship at 19200; NESP-Lib Port defaults to 9600 — pick what matches your pump menu.
        self.baud_var = tk.StringVar(value="19200")
        ttk.Combobox(self, textvariable=self.baud_var, values=BAUD_RATES, state="readonly", width=8).grid(
            row=1, column=5, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self, text="Syringe").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.syringe_var = tk.StringVar(value="BD 10 mL (10 cc)")
        self.syringe_combo = ttk.Combobox(
            self,
            textvariable=self.syringe_var,
            values=list(SYRINGE_PRESETS_MM.keys()),
            state="readonly",
            width=18,
        )
        self.syringe_combo.grid(row=2, column=1, sticky="w", padx=(6, 10), pady=(6, 0))
        self.syringe_combo.bind("<<ComboboxSelected>>", lambda _: self._on_syringe_preset_change())

        ttk.Label(self, text="Custom Diameter (mm)").grid(row=2, column=2, sticky="w", pady=(6, 0))
        self.custom_diameter_var = tk.StringVar(value="14.50")
        ttk.Entry(self, textvariable=self.custom_diameter_var, width=10).grid(row=2, column=3, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self, text="Rate Units").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.rate_units_var = tk.StringVar(value="mL/min")
        ttk.Combobox(self, textvariable=self.rate_units_var, values=RATE_UNITS, state="readonly", width=12).grid(
            row=3, column=1, sticky="w", padx=(6, 10), pady=(6, 0)
        )

        ttk.Label(self, text="Pumping Rate").grid(row=3, column=2, sticky="w", pady=(6, 0))
        self.rate_var = tk.StringVar(value="0.1")
        ttk.Entry(self, textvariable=self.rate_var, width=10).grid(row=3, column=3, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self, text="Dispense Mode").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.dispense_mode_var = tk.StringVar(value="Continuous")
        self.dispense_mode_combo = ttk.Combobox(
            self, textvariable=self.dispense_mode_var, values=DISPENSE_MODES, state="readonly", width=12
        )
        self.dispense_mode_combo.grid(row=4, column=1, sticky="w", padx=(6, 10), pady=(6, 0))
        self.dispense_mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_dispense_volume_visibility())
        self.dispense_mode_var.trace_add("write", lambda *_a: self._sync_dispense_volume_visibility())

        self.volume_ul_var = tk.StringVar(value="0")
        self._volume_ul_label = ttk.Label(self, text="Volume to Dispense (uL)")
        self._volume_ul_entry = ttk.Entry(self, textvariable=self.volume_ul_var, width=10)

        ttk.Label(self, text="Direction").grid(row=6, column=0, sticky="w", pady=(6, 0))
        self.direction_var = tk.StringVar(value="Infuse")
        ttk.Combobox(self, textvariable=self.direction_var, values=["Infuse", "Withdraw"], state="readonly", width=12).grid(
            row=6, column=1, sticky="w", padx=(6, 10), pady=(6, 0)
        )

        ttk.Label(self, text="Volume Dispensed (uL)").grid(row=6, column=2, sticky="w", pady=(6, 0))
        self.dispensed_ul_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.dispensed_ul_var, width=10, state="readonly").grid(
            row=6, column=3, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        ttk.Label(self, text="Time (sec)").grid(row=7, column=0, sticky="w", pady=(6, 0))
        self.time_sec_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.time_sec_var, width=10, state="readonly").grid(
            row=7, column=1, sticky="w", padx=(6, 10), pady=(6, 0)
        )

        ttk.Label(self, text="Total Volume Dispensed (uL)").grid(row=7, column=2, sticky="w", pady=(6, 0))
        self.total_dispensed_ul_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.total_dispensed_ul_var, width=10, state="readonly").grid(
            row=7, column=3, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        btn_row = ttk.Frame(self)
        btn_row.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        for idx in range(8):
            btn_row.columnconfigure(idx, weight=1)
        ttk.Button(btn_row, text="Connect", command=self.connect).grid(row=0, column=0, padx=4, sticky="ew")
        ttk.Button(btn_row, text="Disconnect", command=self.disconnect).grid(row=0, column=1, padx=4, sticky="ew")
        ttk.Button(btn_row, text="Reinitialize", command=self.reinitialize).grid(row=0, column=2, padx=4, sticky="ew")
        ttk.Button(btn_row, text="Apply", command=lambda: self.app.apply_with_mode(self)).grid(row=0, column=3, padx=4, sticky="ew")
        ttk.Button(btn_row, text="Run", command=lambda: self.app.run_with_mode(self)).grid(row=0, column=4, padx=4, sticky="ew")
        ttk.Button(btn_row, text="Stop", command=lambda: self.app.stop_with_mode(self)).grid(row=0, column=5, padx=4, sticky="ew")
        ttk.Button(btn_row, text="Read", command=self.read_status_async).grid(row=0, column=6, padx=4, sticky="ew")
        ttk.Button(btn_row, text="Reset Volume", command=self.reset_volume_async).grid(row=0, column=7, padx=4, sticky="ew")

        btn_row2 = ttk.Frame(self)
        btn_row2.grid(row=9, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(btn_row2, text="Pump Auto-Connect", command=self.auto_connect_pump).pack(side="left", padx=4)

        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(self, textvariable=self.status_var).grid(row=10, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.columnconfigure(1, weight=1)
        self._on_syringe_preset_change()
        self.nickname_var.trace_add("write", lambda *_a: self._on_nickname_changed())
        self._refresh_frame_title()
        self._sync_dispense_volume_visibility()

    def update_port_choices(self, ports: list[str]) -> None:
        current = self.com_var.get().strip()
        self.com_combo["values"] = ports
        if not current and ports:
            self.com_var.set(ports[0])

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _run_in_thread(self, fn) -> None:
        def wrapped() -> None:
            try:
                fn()
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda m=msg: self.set_status(f"Error: {m}"))

        threading.Thread(target=wrapped, daemon=True).start()

    def _require_pump(self) -> Pump:
        if self.connection.pump is None:
            raise RuntimeError("Pump is not connected.")
        return self.connection.pump

    def _get_address(self) -> int:
        return int(self.address_var.get().strip())

    def _on_syringe_preset_change(self) -> None:
        name = self.syringe_var.get()
        value = SYRINGE_PRESETS_MM.get(name)
        if value is not None:
            self.custom_diameter_var.set(f"{value:.2f}")

    def _sync_dispense_volume_visibility(self) -> None:
        """Continuous: hide volume-to-dispense. Volume: show it on its own row."""
        if self.dispense_mode_var.get() == "Volume":
            self._volume_ul_label.grid(row=5, column=0, sticky="w", pady=(6, 0))
            self._volume_ul_entry.grid(row=5, column=1, sticky="w", padx=(6, 10), pady=(6, 0))
        else:
            self._volume_ul_label.grid_remove()
            self._volume_ul_entry.grid_remove()

    def _get_baud_rate(self) -> int:
        return int(self.baud_var.get().strip())

    def connect(self) -> None:
        def work() -> None:
            com_name = self.com_var.get().strip()
            if not com_name:
                raise ValueError("Enter a COM port (example: COM3).")
            address = self._get_address()
            baud_rate = self._get_baud_rate()
            port = None
            try:
                port = Port(com_name, baud_rate=baud_rate)
                pump = Pump(port, address=address, model_number=1000)
                self.connection.port = port
                self.connection.pump = pump
                self.after(0, lambda: self.set_status(f"Connected to {com_name} (addr {address})"))
            except Exception:
                if port is not None:
                    try:
                        port.close()
                    except Exception:
                        pass
                self.connection.port = None
                self.connection.pump = None
                raise

        self._run_in_thread(work)

    def connect_with_params(self, com_name: str, baud_rate: int, address: int) -> None:
        """Open serial like Connect, using explicit parameters (safe from worker threads)."""
        try:
            self.connection.close()
        except Exception:
            pass
        self.connection.port = None
        self.connection.pump = None
        port = None
        try:
            port = Port(com_name, baud_rate=baud_rate)
            pump = Pump(port, address=address, model_number=1000)
            self.connection.port = port
            self.connection.pump = pump
            self.after(0, lambda: self.set_status(f"Connected to {com_name} (addr {address})"))
        except Exception:
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
            self.connection.port = None
            self.connection.pump = None
            raise

    def disconnect_sync(self) -> None:
        try:
            self.connection.close()
        except Exception:
            pass
        self.connection.port = None
        self.connection.pump = None
        self.run_start_ts = None
        self.after(0, lambda: self.set_status("Disconnected"))

    def disconnect(self) -> None:
        def work() -> None:
            self.disconnect_sync()

        self._run_in_thread(work)

    def reinitialize(self) -> None:
        def work() -> None:
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection.port = None
            self.connection.pump = None
            self.run_start_ts = None
            self._polling = False

            def reset_ui() -> None:
                self.set_status("Not connected")
                self.dispensed_ul_var.set("0")
                self.total_dispensed_ul_var.set("0")
                self.time_sec_var.set("0")

            self.after(0, reset_ui)

        self._run_in_thread(work)

    def auto_connect_pump(self) -> None:
        """Pump Auto-Connect: use selected COM port; scan baud + address 0–9, then open connection when found."""
        def work() -> None:
            com_name = self.com_var.get().strip()
            if not com_name:
                self.after(0, lambda: self.set_status("Enter a COM port, then use Pump Auto-Connect."))
                return
            self.after(0, lambda: self.set_status("Pump Auto-Connect: scanning baud rates and addresses 0–9..."))
            for baud in [19200, 9600]:
                for addr in range(10):
                    self.after(0, lambda b=baud, a=addr: self.set_status(
                        f"Trying baud {b}, address {a}..."))
                    if probe_pump_address(com_name, addr, baud):
                        port = None
                        try:
                            port = Port(com_name, baud_rate=baud)
                            pump = Pump(port, address=addr, model_number=0)
                            self.connection.port = port
                            self.connection.pump = pump
                            self.after(0, lambda a=addr: self.address_var.set(str(a)))
                            self.after(0, lambda b=baud: self.baud_var.set(str(b)))
                            self.after(0, lambda a=addr, b=baud: self.set_status(
                                f"Found pump at address {a}, baud {b} on {com_name} — connected!"))
                            return
                        except Exception as exc:
                            if port is not None:
                                try:
                                    port.close()
                                except Exception:
                                    pass
                            self.after(0, lambda a=addr, b=baud, e=str(exc): self.set_status(
                                f"Pump responded at addr {a} baud {b} but connect failed: {e}"))
                            return
            self.after(0, lambda: self.set_status(
                "No pump found. Check cable, power, and that the pump is on."))

        self._run_in_thread(work)

    def snapshot_settings(self) -> dict:
        return {
            "syringe": self.syringe_var.get(),
            "custom_diameter_mm": self.custom_diameter_var.get().strip(),
            "rate_units": self.rate_units_var.get(),
            "rate_value": self.rate_var.get().strip(),
            "dispense_mode": self.dispense_mode_var.get(),
            "volume_ul": self.volume_ul_var.get().strip(),
            "direction": self.direction_var.get(),
        }

    def apply_settings_from_snapshot(self, data: dict) -> None:
        self.syringe_var.set(data["syringe"])
        self._on_syringe_preset_change()
        if data["syringe"] == "Custom":
            self.custom_diameter_var.set(data["custom_diameter_mm"])
        self.rate_units_var.set(data["rate_units"])
        self.rate_var.set(data["rate_value"])
        self.dispense_mode_var.set(data["dispense_mode"])
        self.volume_ul_var.set(data["volume_ul"])
        self.direction_var.set(data["direction"])

    def apply_settings_sync(self) -> None:
        pump = self._require_pump()
        diameter_mm = float(self.custom_diameter_var.get().strip())
        rate_value = float(self.rate_var.get().strip())
        rate_ml_per_min = to_ml_per_min(rate_value, self.rate_units_var.get())
        volume_ul = float(self.volume_ul_var.get().strip())
        volume_ml = max(volume_ul, 0.0) / 1000.0
        direction = PumpingDirection.INFUSE if self.direction_var.get() == "Infuse" else PumpingDirection.WITHDRAW

        pump.syringe_diameter = diameter_mm
        pump.pumping_rate = rate_ml_per_min
        pump.pumping_direction = direction
        if self.dispense_mode_var.get() == "Volume":
            if volume_ml <= 0.0:
                raise ValueError("Volume mode requires a positive 'Volume to Dispense (uL)'.")
            pump.pumping_volume = volume_ml

    def run_sync(self) -> None:
        pump = self._require_pump()
        pump.run(wait_while_running=False)
        self.run_start_ts = time.time()

    def stop_sync(self) -> None:
        pump = self._require_pump()
        pump.stop(wait_while_running=False)
        self.run_start_ts = None

    def read_status_sync(self) -> None:
        pump = self._require_pump()
        status_name = pump.status.name
        infused_ul = pump.volume_infused * 1000.0
        withdrawn_ul = pump.volume_withdrawn * 1000.0
        direction = self.direction_var.get()
        active_ul = infused_ul if direction == "Infuse" else withdrawn_ul
        total_ul = infused_ul + withdrawn_ul

        def update_ui() -> None:
            self.dispensed_ul_var.set(f"{active_ul:.1f}")
            self.total_dispensed_ul_var.set(f"{total_ul:.1f}")
            self.set_status(f"Status: {status_name}")

        self.after(0, update_ui)

    def read_status_async(self) -> None:
        self._run_in_thread(self.read_status_sync)

    def reset_volume_async(self) -> None:
        def work() -> None:
            pump = self._require_pump()
            pump.volume_infused_clear()
            pump.volume_withdrawn_clear()
            self.after(0, lambda: self.dispensed_ul_var.set("0"))
            self.after(0, lambda: self.total_dispensed_ul_var.set("0"))
            self.after(0, lambda: self.set_status("Volume counters reset"))

        self._run_in_thread(work)

    def maybe_poll_live_status(self) -> None:
        if self.connection.pump is None or self._polling:
            return

        self._polling = True

        def work() -> None:
            try:
                self.read_status_sync()
            except Exception:
                pass
            finally:
                self._polling = False

        threading.Thread(target=work, daemon=True).start()

    def update_time_display(self) -> None:
        if self.run_start_ts is None:
            self.time_sec_var.set("0")
            return
        elapsed = int(max(0.0, time.time() - self.run_start_ts))
        self.time_sec_var.set(str(elapsed))


class VacuumPanel(ttk.LabelFrame):
    """Vacuum control using an Arduino serial command (1/0)."""

    def __init__(self, master: tk.Widget, ports: list[str]) -> None:
        super().__init__(master, text="Vacuum Control", padding=10)
        self.serial_conn: Optional[serial.Serial] = None
        self.connected_com: Optional[str] = None
        self.connected_since_ts: Optional[float] = None
        self.is_on = False
        self._build(ports)

    def _build(self, ports: list[str]) -> None:
        ttk.Label(self, text="COM Port").grid(row=0, column=0, sticky="w")
        self.com_var = tk.StringVar(value=(ports[0] if ports else ""))
        self.com_combo = ttk.Combobox(self, textvariable=self.com_var, values=ports, width=12)
        self.com_combo.grid(row=0, column=1, sticky="w", padx=(6, 0))

        ttk.Label(self, text="Baud").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.baud_var = tk.StringVar(value="9600")
        ttk.Entry(self, textvariable=self.baud_var, width=12, state="readonly").grid(
            row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        ttk.Label(self, text="Startup Delay (s)").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.startup_delay_var = tk.StringVar(value="2.0")
        ttk.Entry(self, textvariable=self.startup_delay_var, width=12).grid(
            row=2, column=1, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        conn_row = ttk.Frame(self)
        conn_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        conn_row.columnconfigure(0, weight=1)
        conn_row.columnconfigure(1, weight=1)
        ttk.Button(conn_row, text="Connect Arduino", command=self.connect_arduino).grid(row=0, column=0, padx=(0, 4), sticky="ew")
        ttk.Button(conn_row, text="Disconnect", command=self.disconnect_arduino).grid(row=0, column=1, padx=(4, 0), sticky="ew")

        self.conn_status_var = tk.StringVar(value="Arduino: Disconnected")
        ttk.Label(self, textvariable=self.conn_status_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.toggle_btn = tk.Button(
            self, text="Tap ON", command=self.toggle_vacuum,
            fg="white", bd=0, relief="flat",
            font=("Segoe UI", 10, "bold"),
            padx=10, pady=8, cursor="hand2",
        )
        self.toggle_btn.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self._set_button_color()

        self.status_var = tk.StringVar(value="Vacuum OFF")
        ttk.Label(self, textvariable=self.status_var).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.reply_var = tk.StringVar(value="Arduino reply: (none)")
        ttk.Label(self, textvariable=self.reply_var).grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.columnconfigure(1, weight=1)

    def update_port_choices(self, ports: list[str]) -> None:
        current = self.com_var.get().strip()
        self.com_combo["values"] = ports
        if not current and ports:
            self.com_var.set(ports[0])

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_conn_status(self, text: str) -> None:
        self.conn_status_var.set(text)

    def _set_button_color(self) -> None:
        if self.is_on:
            self.toggle_btn.configure(bg="#C0392B", activebackground="#A93226")
        else:
            self.toggle_btn.configure(bg="#1E8449", activebackground="#196F3D")

    def _set_reply(self, text: str) -> None:
        self.reply_var.set(f"Arduino reply: {text}")

    def _startup_delay_seconds(self) -> float:
        delay = float(self.startup_delay_var.get().strip())
        if delay < 0:
            raise ValueError("Startup Delay must be >= 0.")
        return delay

    def _wait_until_ready(self) -> None:
        if self.connected_since_ts is None:
            return
        delay = self._startup_delay_seconds()
        elapsed = time.time() - self.connected_since_ts
        remaining = delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _ensure_connected(self) -> serial.Serial:
        com_name = self.com_var.get().strip()
        if self.serial_conn is not None and self.serial_conn.is_open:
            if not com_name or self.connected_com == com_name:
                return self.serial_conn
        if not com_name:
            raise ValueError("Select or type a COM port for the vacuum Arduino.")

        self._close_serial()
        self.serial_conn = serial.Serial(port=com_name, baudrate=9600, timeout=1)
        self.connected_com = com_name
        self.connected_since_ts = time.time()
        self._set_conn_status(f"Arduino: Connected on {com_name} @ 9600")
        return self.serial_conn

    def _close_serial(self) -> None:
        if self.serial_conn is not None:
            try:
                self.serial_conn.close()
            except Exception:
                pass
        self.serial_conn = None
        self.connected_com = None
        self.connected_since_ts = None
        self._set_conn_status("Arduino: Disconnected")

    def open_serial_explicit(self, com: str, baud: int = 9600) -> None:
        """Open vacuum serial on ``com`` (for recipe runner; does not require com_var yet)."""
        self._close_serial()
        self.serial_conn = serial.Serial(port=com, baudrate=baud, timeout=1)
        self.connected_com = com
        self.connected_since_ts = time.time()
        self.after(0, lambda c=com: self.com_var.set(c))
        self.after(0, lambda c=com: self._set_conn_status(f"Arduino: Connected on {c} @ {baud}"))

    def close_serial_sync(self) -> None:
        self._close_serial()
        self.is_on = False
        self.after(0, lambda: self.toggle_btn.config(text="Tap ON"))
        self.after(0, self._set_button_color)
        self.after(0, lambda: self._set_status("Vacuum OFF"))
        self.after(0, lambda: self._set_reply("(none)"))

    def send_vacuum_sync(self, on: bool) -> None:
        conn = self._ensure_connected()
        self._wait_until_ready()
        conn.write(("1" if on else "0").encode("ascii"))
        conn.flush()
        self.is_on = on
        self.after(0, lambda o=on: self.toggle_btn.config(text="Tap OFF" if o else "Tap ON"))
        self.after(0, self._set_button_color)
        self.after(0, lambda o=on: self._set_status("Vacuum ON (recipe)" if o else "Vacuum OFF (recipe)"))

    def _send_value(self, value: str) -> None:
        conn = self._ensure_connected()
        self._wait_until_ready()
        conn.write(value.encode("ascii"))
        conn.flush()
        self._read_reply_async()

    def _read_reply_async(self) -> None:
        conn = self.serial_conn
        if conn is None or not conn.is_open:
            return

        def work() -> None:
            try:
                time.sleep(0.08)
                line = conn.readline().decode("utf-8", errors="ignore").strip()
                if line:
                    self.after(0, lambda: self._set_reply(line))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def connect_arduino(self) -> None:
        def work() -> None:
            conn = self._ensure_connected()
            self._wait_until_ready()
            if conn.in_waiting:
                _ = conn.read(conn.in_waiting)
            self.after(0, lambda: self._set_status("Arduino connected and ready"))
            self.after(0, lambda: self._set_reply("ready"))

        threading.Thread(target=work, daemon=True).start()

    def disconnect_arduino(self) -> None:
        def work() -> None:
            self._close_serial()
            self.is_on = False
            self.after(0, lambda: self.toggle_btn.config(text="Tap ON"))
            self.after(0, self._set_button_color)
            self.after(0, lambda: self._set_status("Vacuum OFF"))
            self.after(0, lambda: self._set_reply("(none)"))

        threading.Thread(target=work, daemon=True).start()

    def toggle_vacuum(self) -> None:
        def work() -> None:
            try:
                if self.is_on:
                    self._send_value("0")
                    self.is_on = False
                    self.after(0, lambda: self.toggle_btn.config(text="Tap ON"))
                    self.after(0, self._set_button_color)
                    self.after(0, lambda: self._set_status("Vacuum OFF (sent 0)"))
                else:
                    self._send_value("1")
                    self.is_on = True
                    self.after(0, lambda: self.toggle_btn.config(text="Tap OFF"))
                    self.after(0, self._set_button_color)
                    self.after(0, lambda: self._set_status("Vacuum ON (sent 1)"))
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda m=msg: self._set_status(f"Error: {m}"))

        threading.Thread(target=work, daemon=True).start()

    def force_off(self) -> None:
        def work() -> None:
            try:
                if self.is_on:
                    self._send_value("0")
                self.is_on = False
                self.after(0, lambda: self.toggle_btn.config(text="Tap ON"))
                self.after(0, self._set_button_color)
                self.after(0, lambda: self._set_status("Vacuum OFF (forced)"))
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda m=msg: self._set_status(f"Error forcing OFF: {m}"))

        threading.Thread(target=work, daemon=True).start()

    def close(self) -> None:
        self._close_serial()


def pump_connection_snapshot(panel: "PumpPanel") -> dict[str, Any]:
    return {
        "com": panel.com_var.get().strip(),
        "baud": int(panel.baud_var.get().strip() or "19200"),
        "address": int(panel.address_var.get().strip() or "0"),
    }


class RecipeSequenceEditor(tk.Toplevel):
    """Second-level window to build ordered steps (COM/baud/address, pumps, vacuum, delays)."""

    def __init__(self, master: tk.Widget, app: "PumpControllerApp", recipe: dict[str, Any], on_saved: Callable[[], None]) -> None:
        super().__init__(master)
        self.app = app
        self._recipe_id = recipe.get("id")
        self._on_saved = on_saved
        self._steps: list[dict[str, Any]] = copy.deepcopy(recipe.get("steps") or [])

        theme = DARK if app.dark_mode else LIGHT
        self.title(f"Sequence — {recipe.get('name', 'Recipe')}")
        self.transient(master)
        self.geometry("780x620")
        self.minsize(640, 480)
        self.configure(bg=theme["bg"])

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text=(
                "Build the sequence from top to bottom. Drag a step in the list to reorder it "
                "(or use Move up / down). Double-click a step or use Edit step… to change it. "
                "Steps run in order when you use “Run sequence” on this recipe."
            ),
            wraplength=720,
        ).pack(anchor="w", pady=(0, 8))

        list_fr = ttk.Frame(outer)
        list_fr.pack(fill="both", expand=True, pady=(0, 8))
        list_fr.rowconfigure(0, weight=1)
        list_fr.columnconfigure(0, weight=1)

        self._lb = tk.Listbox(
            list_fr,
            height=14,
            bg=theme["input"],
            fg=theme["fg"],
            selectbackground=theme["btn"],
            selectforeground=theme["fg"],
            highlightthickness=0,
            borderwidth=1,
            relief="solid",
        )
        self._lb.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(list_fr, orient="vertical", command=self._lb.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._lb.configure(yscrollcommand=sb.set)

        self._dnd_start: Optional[int] = None
        self._lb.bind("<ButtonPress-1>", self._dnd_press)
        self._lb.bind("<ButtonRelease-1>", self._dnd_release)
        self._lb.bind("<Double-Button-1>", self._on_step_double_click)

        move_fr = ttk.Frame(outer)
        move_fr.pack(fill="x", pady=(0, 8))
        ttk.Button(move_fr, text="Move up", command=self._move_up).pack(side="left", padx=(0, 6))
        ttk.Button(move_fr, text="Move down", command=self._move_down).pack(side="left", padx=(0, 6))
        ttk.Button(move_fr, text="Edit step…", command=self._edit_selected_step).pack(side="left", padx=(0, 6))
        ttk.Button(move_fr, text="Remove step", command=self._remove).pack(side="left", padx=(0, 6))
        ttk.Button(move_fr, text="Step label…", command=self._label_selected_step).pack(side="left", padx=(16, 6))
        ttk.Button(move_fr, text="Clear step label", command=self._clear_step_label).pack(side="left", padx=(0, 6))

        add_fr = ttk.LabelFrame(outer, text="Add step", padding=8)
        add_fr.pack(fill="x", pady=(0, 8))
        row1 = ttk.Frame(add_fr)
        row1.pack(fill="x")
        ttk.Button(row1, text="Delay…", command=self._add_delay).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(row1, text="Connect pump…", command=self._add_connect_pump).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(row1, text="Disconnect pump…", command=self._add_disconnect_pump).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(row1, text="Apply pump…", command=self._add_apply_pump).pack(side="left", padx=(0, 4), pady=2)
        row2 = ttk.Frame(add_fr)
        row2.pack(fill="x")
        ttk.Button(row2, text="Run pump…", command=self._add_run_pump).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(row2, text="Stop pump…", command=self._add_stop_pump).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(row2, text="Vacuum connect…", command=self._add_vacuum_connect).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(row2, text="Vacuum disconnect", command=self._add_vacuum_disconnect).pack(side="left", padx=(0, 4), pady=2)
        row3 = ttk.Frame(add_fr)
        row3.pack(fill="x")
        ttk.Button(row3, text="Vacuum ON", command=lambda: self._push_step({"type": "vacuum_on"})).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(row3, text="Vacuum OFF", command=lambda: self._push_step({"type": "vacuum_off"})).pack(side="left", padx=(0, 4), pady=2)

        bot = ttk.Frame(outer)
        bot.pack(fill="x")
        ttk.Button(bot, text="Save sequence to recipe", command=self._save).pack(side="left", padx=(0, 8))
        ttk.Button(bot, text="Close", command=self.destroy).pack(side="left")

        self.app._sequence_editor = self
        self._refresh_lb()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def destroy(self) -> None:  # type: ignore[override]
        if getattr(self.app, "_sequence_editor", None) is self:
            self.app._sequence_editor = None
        super().destroy()

    def _theme_for_listbox(self) -> dict:
        return DARK if self.app.dark_mode else LIGHT

    def on_parent_theme_changed(self) -> None:
        theme = self._theme_for_listbox()
        self.configure(bg=theme["bg"])
        self._lb.configure(
            bg=theme["input"],
            fg=theme["fg"],
            selectbackground=theme["btn"],
            selectforeground=theme["fg"],
        )

    def _refresh_lb(self) -> None:
        nicks = self.app.pump_nickname_map()
        self._lb.delete(0, tk.END)
        for i, st in enumerate(self._steps):
            self._lb.insert(tk.END, format_step_list_line(i, st, nicks))

    def _dnd_press(self, event: tk.Event) -> None:
        if event.widget != self._lb:
            return
        n = len(self._steps)
        if n == 0:
            self._dnd_start = None
            return
        idx = self._lb.nearest(event.y)
        if 0 <= idx < n:
            self._dnd_start = idx
        else:
            self._dnd_start = None

    def _dnd_release(self, event: tk.Event) -> None:
        if self._dnd_start is None:
            return
        if event.widget != self._lb:
            self._dnd_start = None
            return
        n = len(self._steps)
        si = self._dnd_start
        self._dnd_start = None
        if n == 0 or si < 0 or si >= n:
            return
        ei = self._lb.nearest(event.y)
        if ei < 0 or ei >= n:
            return
        if si == ei:
            return
        item = self._steps.pop(si)
        if ei > si:
            ei -= 1
        self._steps.insert(ei, item)
        self._refresh_lb()
        self._lb.selection_clear(0, tk.END)
        self._lb.selection_set(ei)
        self._lb.see(ei)

    def _sel(self) -> Optional[int]:
        s = self._lb.curselection()
        return int(s[0]) if s else None

    def _push_step(self, step: dict[str, Any]) -> None:
        self._steps.append(step)
        self._refresh_lb()
        self._lb.selection_clear(0, tk.END)
        self._lb.selection_set(tk.END)
        self._lb.see(tk.END)

    def _commit_step(self, edit_index: Optional[int], new_step: dict[str, Any]) -> None:
        """Append a new step, or replace the step at edit_index (preserves step label)."""
        new_step = copy.deepcopy(new_step)
        if edit_index is not None and 0 <= edit_index < len(self._steps):
            old = self._steps[edit_index]
            lab = (old.get("label") or old.get("step_label") or "").strip()
            if lab:
                new_step["label"] = lab
            self._steps[edit_index] = new_step
            self._refresh_lb()
            self._lb.selection_clear(0, tk.END)
            self._lb.selection_set(edit_index)
            self._lb.see(edit_index)
        else:
            self._push_step(new_step)

    def _on_step_double_click(self, event: tk.Event) -> None:
        self._dnd_start = None
        if event.widget != self._lb:
            return
        n = len(self._steps)
        if n == 0:
            return
        idx = self._lb.nearest(event.y)
        if idx < 0 or idx >= n:
            return
        self._lb.selection_clear(0, tk.END)
        self._lb.selection_set(idx)
        self._edit_step_at(idx)

    def _edit_selected_step(self) -> None:
        self._edit_step_at(None)

    def _edit_step_at(self, index: Optional[int]) -> None:
        if index is None:
            index = self._sel()
        if index is None or index < 0 or index >= len(self._steps):
            messagebox.showinfo("Sequence", "Select a step in the list to edit.", parent=self)
            return
        st = self._steps[index]
        t = st.get("type")
        if t == "delay":
            try:
                cur = float(st.get("seconds", 0) or 0)
            except (TypeError, ValueError):
                cur = 0.0
            v = simpledialog.askfloat(
                "Delay",
                "Seconds to wait:",
                parent=self,
                minvalue=0.0,
                initialvalue=cur,
            )
            if v is not None:
                self._commit_step(index, {"type": "delay", "seconds": float(v)})
        elif t == "connect_pump":
            self._open_connect_pump_dialog(edit_index=index, initial=st)
        elif t == "disconnect_pump":
            try:
                cur_p = int(st.get("pump", 1))
            except (TypeError, ValueError):
                cur_p = 1
            cur_p = max(1, min(3, cur_p))
            p = simpledialog.askinteger(
                "Disconnect pump",
                "Pump number (1–3):",
                parent=self,
                minvalue=1,
                maxvalue=3,
                initialvalue=cur_p,
            )
            if p is not None:
                self._commit_step(index, {"type": "disconnect_pump", "pump": int(p)})
        elif t == "apply_pump":
            self._open_apply_pump_dialog(edit_index=index, initial=st)
        elif t == "run_pump":
            try:
                cur_p = int(st.get("pump", 1))
            except (TypeError, ValueError):
                cur_p = 1
            cur_p = max(1, min(3, cur_p))
            p = simpledialog.askinteger(
                "Run pump",
                "Pump number (1–3):",
                parent=self,
                minvalue=1,
                maxvalue=3,
                initialvalue=cur_p,
            )
            if p is not None:
                self._commit_step(index, {"type": "run_pump", "pump": int(p)})
        elif t == "stop_pump":
            try:
                cur_p = int(st.get("pump", 1))
            except (TypeError, ValueError):
                cur_p = 1
            cur_p = max(1, min(3, cur_p))
            p = simpledialog.askinteger(
                "Stop pump",
                "Pump number (1–3):",
                parent=self,
                minvalue=1,
                maxvalue=3,
                initialvalue=cur_p,
            )
            if p is not None:
                self._commit_step(index, {"type": "stop_pump", "pump": int(p)})
        elif t == "vacuum_connect":
            self._open_vacuum_connect_dialog(edit_index=index, initial=st)
        elif t in ("vacuum_disconnect", "vacuum_on", "vacuum_off"):
            messagebox.showinfo(
                "Edit step",
                "This step has no settings to change. Use Remove step if you no longer need it.",
                parent=self,
            )
        else:
            messagebox.showwarning("Edit step", f"Unknown or unsupported step type: {t!r}", parent=self)

    def _move_up(self) -> None:
        i = self._sel()
        if i is None or i <= 0:
            return
        self._steps[i - 1], self._steps[i] = self._steps[i], self._steps[i - 1]
        self._refresh_lb()
        self._lb.selection_set(i - 1)

    def _move_down(self) -> None:
        i = self._sel()
        if i is None or i >= len(self._steps) - 1:
            return
        self._steps[i + 1], self._steps[i] = self._steps[i], self._steps[i + 1]
        self._refresh_lb()
        self._lb.selection_set(i + 1)

    def _remove(self) -> None:
        i = self._sel()
        if i is None:
            return
        self._steps.pop(i)
        self._refresh_lb()

    def _label_selected_step(self) -> None:
        i = self._sel()
        if i is None:
            messagebox.showinfo("Sequence", "Select a step in the list first.", parent=self)
            return
        st = self._steps[i]
        cur = (st.get("label") or st.get("step_label") or "").strip()
        v = simpledialog.askstring(
            "Step label",
            "Short label for this step (shown in the list):",
            initialvalue=cur,
            parent=self,
        )
        if v is None:
            return
        v = v.strip()
        if v:
            st["label"] = v
        else:
            st.pop("label", None)
            st.pop("step_label", None)
        self._refresh_lb()
        self._lb.selection_set(i)

    def _clear_step_label(self) -> None:
        i = self._sel()
        if i is None:
            messagebox.showinfo("Sequence", "Select a step first.", parent=self)
            return
        self._steps[i].pop("label", None)
        self._steps[i].pop("step_label", None)
        self._refresh_lb()
        self._lb.selection_set(i)

    def _com_port_values(self, preferred: str = "") -> list[str]:
        ports = list(detected_port_names())
        p = preferred.strip().upper()
        if p and p not in ports:
            ports.insert(0, preferred.strip())
        return ports if ports else ([preferred.strip()] if preferred.strip() else ["COM1"])

    def _add_delay(self) -> None:
        v = simpledialog.askfloat("Delay", "Seconds to wait:", parent=self, minvalue=0.0, initialvalue=1.0)
        if v is not None:
            self._push_step({"type": "delay", "seconds": float(v)})

    def _add_connect_pump(self) -> None:
        self._open_connect_pump_dialog()

    def _open_connect_pump_dialog(
        self, edit_index: Optional[int] = None, initial: Optional[dict[str, Any]] = None
    ) -> None:
        is_edit = edit_index is not None
        d = tk.Toplevel(self)
        d.title("Edit connect pump" if is_edit else "Connect pump")
        d.transient(self)
        fr = ttk.Frame(d, padding=10)
        fr.pack(fill="both", expand=True)
        pv = tk.StringVar(value="1")
        ttk.Label(fr, text="Pump (1–3)").grid(row=0, column=0, sticky="w")
        ttk.Combobox(fr, textvariable=pv, values=["1", "2", "3"], width=6, state="readonly").grid(row=0, column=1, sticky="w", padx=6)
        com0 = "COM8"
        if initial:
            try:
                pv.set(str(int(initial.get("pump", 1))))
            except (TypeError, ValueError):
                pv.set("1")
            com0 = str(initial.get("com") or "COM8").strip() or "COM8"
        cv = tk.StringVar(value=com0)
        ttk.Label(fr, text="COM port").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(
            fr, textvariable=cv, values=self._com_port_values(com0), width=14
        ).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        baud0 = "19200"
        if initial:
            try:
                baud0 = str(int(initial.get("baud", 19200)))
            except (TypeError, ValueError):
                baud0 = "19200"
        bv = tk.StringVar(value=baud0)
        ttk.Label(fr, text="Baud").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(fr, textvariable=bv, values=list(BAUD_RATES), width=8, state="readonly").grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        addr0 = "0"
        if initial:
            try:
                addr0 = str(int(initial.get("address", 0)))
            except (TypeError, ValueError):
                addr0 = "0"
        av = tk.StringVar(value=addr0)
        ttk.Label(fr, text="Address").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(fr, textvariable=av, width=8).grid(row=3, column=1, sticky="w", padx=6, pady=(6, 0))

        def ok() -> None:
            try:
                p = int(pv.get())
                baud = int(bv.get())
                addr = int(av.get())
            except ValueError:
                messagebox.showerror("Connect pump", "Invalid number.", parent=d)
                return
            if p not in (1, 2, 3):
                return
            self._commit_step(
                edit_index,
                {"type": "connect_pump", "pump": p, "com": cv.get().strip(), "baud": baud, "address": addr},
            )
            d.destroy()

        ttk.Button(fr, text="Cancel", command=d.destroy).grid(row=4, column=0, pady=(12, 0), sticky="w")
        ttk.Button(fr, text=("Save" if is_edit else "Add"), command=ok).grid(row=4, column=1, pady=(12, 0), sticky="e")

    def _add_disconnect_pump(self) -> None:
        p = simpledialog.askinteger("Disconnect pump", "Pump number (1–3):", parent=self, minvalue=1, maxvalue=3)
        if p is not None:
            self._push_step({"type": "disconnect_pump", "pump": int(p)})

    def _add_apply_pump(self) -> None:
        self._open_apply_pump_dialog()

    def _open_apply_pump_dialog(
        self, edit_index: Optional[int] = None, initial: Optional[dict[str, Any]] = None
    ) -> None:
        is_edit = edit_index is not None
        d = tk.Toplevel(self)
        d.title("Edit apply pump step" if is_edit else "Apply pump — full settings")
        d.transient(self)
        d.geometry("480x420")
        fr = ttk.Frame(d, padding=12)
        fr.pack(fill="both", expand=True)

        pv = tk.StringVar(value="1")
        ttk.Label(fr, text="Pump (1–3)").grid(row=0, column=0, sticky="w")
        pump_cb = ttk.Combobox(fr, textvariable=pv, values=["1", "2", "3"], width=6, state="readonly")
        pump_cb.grid(row=0, column=1, sticky="w", padx=6)

        syringe_var = tk.StringVar(value="BD 10 mL (10 cc)")
        custom_d_var = tk.StringVar(value="14.50")
        rate_units_var = tk.StringVar(value="mL/min")
        rate_var = tk.StringVar(value="0.1")
        dispense_var = tk.StringVar(value="Continuous")
        volume_ul_var = tk.StringVar(value="0")
        direction_var = tk.StringVar(value="Infuse")

        if initial and initial.get("type") == "apply_pump":
            try:
                pv.set(str(max(1, min(3, int(initial.get("pump", 1))))))
            except (TypeError, ValueError):
                pv.set("1")
            s = initial.get("settings") if isinstance(initial.get("settings"), dict) else {}
            syringe_var.set(str(s.get("syringe") or syringe_var.get()))
            custom_d_var.set(str(s.get("custom_diameter_mm") or custom_d_var.get()))
            rate_units_var.set(str(s.get("rate_units") or rate_units_var.get()))
            rate_var.set(str(s.get("rate_value") or rate_var.get()))
            dispense_var.set(str(s.get("dispense_mode") or dispense_var.get()))
            volume_ul_var.set(str(s.get("volume_ul", volume_ul_var.get())))
            direction_var.set(str(s.get("direction") or direction_var.get()))

        def on_syringe_selected(_: Any = None) -> None:
            name = syringe_var.get()
            val = SYRINGE_PRESETS_MM.get(name)
            if val is not None:
                custom_d_var.set(f"{val:.2f}")

        row = 1
        ttk.Label(fr, text="Syringe").grid(row=row, column=0, sticky="w", pady=(8, 0))
        syringe_cb = ttk.Combobox(
            fr,
            textvariable=syringe_var,
            values=list(SYRINGE_PRESETS_MM.keys()),
            state="readonly",
            width=22,
        )
        syringe_cb.grid(row=row, column=1, sticky="w", padx=6, pady=(8, 0))
        syringe_cb.bind("<<ComboboxSelected>>", on_syringe_selected)
        row += 1
        ttk.Label(fr, text="Custom diameter (mm)").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(fr, textvariable=custom_d_var, width=12).grid(row=row, column=1, sticky="w", padx=6, pady=(6, 0))
        row += 1
        ttk.Label(fr, text="Rate units").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(
            fr, textvariable=rate_units_var, values=RATE_UNITS, state="readonly", width=12
        ).grid(row=row, column=1, sticky="w", padx=6, pady=(6, 0))
        row += 1
        ttk.Label(fr, text="Pumping rate").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(fr, textvariable=rate_var, width=12).grid(row=row, column=1, sticky="w", padx=6, pady=(6, 0))
        row += 1
        disp_row = row
        ttk.Label(fr, text="Dispense mode").grid(row=disp_row, column=0, sticky="w", pady=(6, 0))
        disp_apply_cb = ttk.Combobox(
            fr, textvariable=dispense_var, values=DISPENSE_MODES, state="readonly", width=12
        )
        disp_apply_cb.grid(row=disp_row, column=1, sticky="w", padx=6, pady=(6, 0))

        vol_block = ttk.Frame(fr)
        ttk.Label(vol_block, text="Volume to dispense (µL)").pack(side="left")
        ttk.Entry(vol_block, textvariable=volume_ul_var, width=12).pack(side="left", padx=(8, 0))

        dir_lbl = ttk.Label(fr, text="Direction")
        dir_apply_cb = ttk.Combobox(
            fr, textvariable=direction_var, values=["Infuse", "Withdraw"], state="readonly", width=12
        )

        load_btn_holder: list[Optional[ttk.Button]] = [None]
        btn_fr_holder: list[Optional[ttk.Frame]] = [None]

        def layout_apply_dialog_below_dispense() -> None:
            if dispense_var.get() == "Volume":
                vol_block.grid(row=disp_row + 1, column=0, columnspan=2, sticky="w", pady=(6, 0))
                dr = disp_row + 2
            else:
                vol_block.grid_remove()
                dr = disp_row + 1
            dir_lbl.grid(row=dr, column=0, sticky="w", pady=(6, 0))
            dir_apply_cb.grid(row=dr, column=1, sticky="w", padx=6, pady=(6, 0))
            lr = dr + 1
            if load_btn_holder[0] is not None:
                load_btn_holder[0].grid(row=lr, column=0, columnspan=2, sticky="w", pady=(12, 0))
            if btn_fr_holder[0] is not None:
                btn_fr_holder[0].grid(row=lr + 1, column=0, columnspan=2, pady=(14, 0), sticky="ew")

        disp_apply_cb.bind("<<ComboboxSelected>>", lambda _e: layout_apply_dialog_below_dispense())

        def load_from_main() -> None:
            try:
                p = int(pv.get())
            except ValueError:
                return
            snap = self.app.panels[p - 1].snapshot_settings()
            syringe_var.set(snap["syringe"])
            custom_d_var.set(snap["custom_diameter_mm"])
            rate_units_var.set(snap["rate_units"])
            rate_var.set(snap["rate_value"])
            dispense_var.set(snap["dispense_mode"])
            volume_ul_var.set(snap["volume_ul"])
            direction_var.set(snap["direction"])
            layout_apply_dialog_below_dispense()

        load_btn = ttk.Button(fr, text="Load from main window for this pump", command=load_from_main)
        load_btn_holder[0] = load_btn

        def ok() -> None:
            try:
                p = int(pv.get())
            except ValueError:
                messagebox.showerror("Apply pump", "Invalid pump number.", parent=d)
                return
            if p not in (1, 2, 3):
                return
            settings: dict[str, Any] = {
                "syringe": syringe_var.get(),
                "custom_diameter_mm": custom_d_var.get().strip(),
                "rate_units": rate_units_var.get(),
                "rate_value": rate_var.get().strip(),
                "dispense_mode": dispense_var.get(),
                "volume_ul": volume_ul_var.get().strip(),
                "direction": direction_var.get(),
            }
            self._commit_step(edit_index, {"type": "apply_pump", "pump": p, "settings": settings})
            d.destroy()

        btn_fr = ttk.Frame(fr)
        btn_fr_holder[0] = btn_fr
        ttk.Button(btn_fr, text="Cancel", command=d.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(btn_fr, text=("Save changes" if is_edit else "Add step"), command=ok).pack(side="left")

        layout_apply_dialog_below_dispense()
        dispense_var.trace_add("write", lambda *_a: layout_apply_dialog_below_dispense())
        if not is_edit:
            pv.trace_add("write", lambda *_a: load_from_main())
            load_from_main()

    def _add_run_pump(self) -> None:
        p = simpledialog.askinteger("Run pump", "Pump number (1–3):", parent=self, minvalue=1, maxvalue=3)
        if p is not None:
            self._push_step({"type": "run_pump", "pump": int(p)})

    def _add_stop_pump(self) -> None:
        p = simpledialog.askinteger("Stop pump", "Pump number (1–3):", parent=self, minvalue=1, maxvalue=3)
        if p is not None:
            self._push_step({"type": "stop_pump", "pump": int(p)})

    def _add_vacuum_connect(self) -> None:
        self._open_vacuum_connect_dialog()

    def _open_vacuum_connect_dialog(
        self, edit_index: Optional[int] = None, initial: Optional[dict[str, Any]] = None
    ) -> None:
        is_edit = edit_index is not None
        d = tk.Toplevel(self)
        d.title("Edit vacuum connect" if is_edit else "Vacuum connect (Arduino)")
        d.transient(self)
        d.geometry("360x140")
        fr = ttk.Frame(d, padding=12)
        fr.pack(fill="both", expand=True)
        com0 = "COM6"
        baud0 = "9600"
        if initial and initial.get("type") == "vacuum_connect":
            com0 = str(initial.get("com") or "COM6").strip() or "COM6"
            try:
                baud0 = str(int(initial.get("baud", 9600)))
            except (TypeError, ValueError):
                baud0 = "9600"
        cv = tk.StringVar(value=com0)
        bv = tk.StringVar(value=baud0)
        ttk.Label(fr, text="Arduino COM port").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            fr,
            textvariable=cv,
            values=self._com_port_values(com0),
            width=16,
        ).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(fr, text="Baud rate").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            fr,
            textvariable=bv,
            values=list(VACUUM_BAUD_RATES),
            width=10,
            state="readonly",
        ).grid(row=1, column=1, sticky="w", padx=8, pady=(8, 0))
        ttk.Label(
            fr,
            text="(Vacuum sketch default is 9600 — match your Arduino code.)",
            wraplength=300,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        def ok() -> None:
            com = cv.get().strip()
            if not com:
                messagebox.showerror("Vacuum connect", "Enter a COM port.", parent=d)
                return
            try:
                baud = int(bv.get().strip())
            except ValueError:
                messagebox.showerror("Vacuum connect", "Invalid baud.", parent=d)
                return
            self._commit_step(edit_index, {"type": "vacuum_connect", "com": com, "baud": baud})
            d.destroy()

        bf = ttk.Frame(fr)
        bf.grid(row=3, column=0, columnspan=2, pady=(14, 0), sticky="w")
        ttk.Button(bf, text="Cancel", command=d.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(bf, text=("Save" if is_edit else "Add step"), command=ok).pack(side="left")

    def _add_vacuum_disconnect(self) -> None:
        self._push_step({"type": "vacuum_disconnect"})

    def _save(self) -> None:
        rid = self._recipe_id
        if not rid:
            messagebox.showerror("Sequence", "Recipe has no id; save the recipe from the main Recipes window first.", parent=self)
            return
        all_recipes = load_stored_recipes()
        for r in all_recipes:
            if r.get("id") == rid:
                r["steps"] = copy.deepcopy(self._steps)
                save_stored_recipes(all_recipes)
                self._on_saved()
                messagebox.showinfo("Sequence", "Sequence saved to this recipe.", parent=self)
                return
        messagebox.showerror("Sequence", "Recipe not found in file.", parent=self)


class RecipesPanel(ttk.Frame):
    """Save / load pump 1–3 parameter sets; apply to panels or apply+run connected pumps."""

    def __init__(self, master: tk.Widget, app: "PumpControllerApp") -> None:
        super().__init__(master, padding=4)
        self.app = app
        self._recipes: list[dict[str, Any]] = []

        ttk.Label(self, text="Recipes", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(
            self,
            text="Saves pump settings + COM/baud/address. Use “Edit sequence…” for delays, vacuum, "
            "and ordered connect/apply/run steps. If a sequence exists, “Run sequence” executes it.",
            wraplength=420,
        ).pack(anchor="w", pady=(4, 8))

        list_frame = ttk.Frame(self)
        list_frame.pack(fill="both", expand=True, pady=(0, 8))
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        theme = DARK if app.dark_mode else LIGHT
        self._listbox = tk.Listbox(
            list_frame,
            height=9,
            bg=theme["input"],
            fg=theme["fg"],
            selectbackground=theme["btn"],
            selectforeground=theme["fg"],
            highlightthickness=0,
            borderwidth=1,
            relief="solid",
            activestyle="dotbox",
        )
        self._listbox.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self._listbox.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._listbox.configure(yscrollcommand=sb.set)

        name_row = ttk.Frame(self)
        name_row.pack(fill="x", pady=(0, 6))
        ttk.Label(name_row, text="New name:").pack(side="left")
        self._name_var = tk.StringVar()
        ttk.Entry(name_row, textvariable=self._name_var, width=30).pack(side="left", padx=(6, 0))

        btn_row1 = ttk.Frame(self)
        btn_row1.pack(fill="x", pady=(0, 4))
        ttk.Button(btn_row1, text="Save from main window", command=self._on_save).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row1, text="Refresh list", command=self.refresh_list).pack(side="left")

        btn_row2 = ttk.Frame(self)
        btn_row2.pack(fill="x", pady=(0, 4))
        ttk.Button(btn_row2, text="Apply to pump panels", command=self._on_apply).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row2, text="Apply + Run all", command=self._on_run).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row2, text="Run sequence", command=self._on_run_sequence).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row2, text="Delete", command=self._on_delete).pack(side="left")

        btn_row3 = ttk.Frame(self)
        btn_row3.pack(fill="x", pady=(0, 4))
        ttk.Button(btn_row3, text="Edit sequence…", command=self._open_sequence_editor).pack(side="left")

        self._status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._status_var, wraplength=420).pack(anchor="w", pady=(8, 0))

        self.refresh_list()

    def on_theme_changed(self) -> None:
        theme = DARK if self.app.dark_mode else LIGHT
        self._listbox.configure(
            bg=theme["input"],
            fg=theme["fg"],
            selectbackground=theme["btn"],
            selectforeground=theme["fg"],
        )

    def refresh_list(self) -> None:
        self._recipes = load_stored_recipes()
        self._listbox.delete(0, tk.END)
        for r in self._recipes:
            self._listbox.insert(tk.END, recipe_display_name(r))
        try:
            self.app.refresh_quick_recipe_combo()
        except (tk.TclError, AttributeError):
            pass

    def _set_status(self, text: str) -> None:
        self._status_var.set(text)

    def _selected_index(self) -> Optional[int]:
        sel = self._listbox.curselection()
        if not sel:
            return None
        return int(sel[0])

    def _selected_recipe(self) -> Optional[dict[str, Any]]:
        idx = self._selected_index()
        if idx is None or idx >= len(self._recipes):
            return None
        return self._recipes[idx]

    def _on_save(self) -> None:
        name = self._name_var.get().strip()
        if not name:
            messagebox.showwarning("Recipes", "Enter a name for the new recipe.", parent=self.winfo_toplevel())
            return
        recipe: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "name": name,
            "pump1": self.app.panels[0].snapshot_settings(),
            "pump2": self.app.panels[1].snapshot_settings(),
            "pump3": self.app.panels[2].snapshot_settings(),
            "pump1_conn": pump_connection_snapshot(self.app.panels[0]),
            "pump2_conn": pump_connection_snapshot(self.app.panels[1]),
            "pump3_conn": pump_connection_snapshot(self.app.panels[2]),
            "pump_labels": {
                "1": self.app.panels[0].nickname_var.get().strip(),
                "2": self.app.panels[1].nickname_var.get().strip(),
                "3": self.app.panels[2].nickname_var.get().strip(),
            },
        }
        self._recipes.append(recipe)
        save_stored_recipes(self._recipes)
        self.refresh_list()
        self._set_status(f"Saved “{name}”.")
        self._name_var.set("")

    def _on_apply(self) -> None:
        rec = self._selected_recipe()
        if rec is None:
            messagebox.showinfo("Recipes", "Select a recipe in the list.", parent=self.winfo_toplevel())
            return
        self.app.apply_recipe_to_panels(rec)
        self._set_status(f"Applied “{rec.get('name', '')}” to pump panels (settings + COM/baud/address).")

    def _on_run(self) -> None:
        rec = self._selected_recipe()
        if rec is None:
            messagebox.showinfo("Recipes", "Select a recipe in the list.", parent=self.winfo_toplevel())
            return
        if rec.get("steps"):
            messagebox.showinfo(
                "Recipes",
                "This recipe has a saved sequence. Use “Run sequence” to execute it in order.",
                parent=self.winfo_toplevel(),
            )
            return
        self.app.run_recipe(rec)
        self._set_status(f"Started “{rec.get('name', '')}” on connected pumps (see main window).")

    def _on_run_sequence(self) -> None:
        rec = self._selected_recipe()
        if rec is None:
            messagebox.showinfo("Recipes", "Select a recipe in the list.", parent=self.winfo_toplevel())
            return
        steps = rec.get("steps")
        if not isinstance(steps, list) or len(steps) == 0:
            messagebox.showinfo(
                "Recipes",
                "No steps yet. Select the recipe and click “Edit sequence…” to add steps.",
                parent=self.winfo_toplevel(),
            )
            return
        self.app.run_recipe_sequence(rec)
        self._set_status(f"Running sequence “{rec.get('name', '')}”…")

    def _open_sequence_editor(self) -> None:
        rec = self._selected_recipe()
        if rec is None:
            messagebox.showinfo("Recipes", "Select a recipe first.", parent=self.winfo_toplevel())
            return
        if not rec.get("id"):
            messagebox.showwarning("Recipes", "Recipe has no id — save again from this window.", parent=self.winfo_toplevel())
            return
        RecipeSequenceEditor(self.winfo_toplevel(), self.app, rec, self.refresh_list)

    def _on_delete(self) -> None:
        idx = self._selected_index()
        if idx is None or idx >= len(self._recipes):
            messagebox.showinfo("Recipes", "Select a recipe to delete.", parent=self.winfo_toplevel())
            return
        rec = self._recipes[idx]
        if not messagebox.askyesno(
            "Recipes",
            f"Delete recipe “{rec.get('name', '')}”?",
            parent=self.winfo_toplevel(),
        ):
            return
        self._recipes.pop(idx)
        save_stored_recipes(self._recipes)
        self.refresh_list()
        self._set_status("Recipe deleted.")


class PumpControllerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Syringe Pumps Control (NE-1000)")
        self.geometry("1180x830")
        self.minsize(1080, 760)

        self.mode_var = tk.StringVar(value="Individual mode")
        self.switch_together_var = tk.BooleanVar(value=False)
        self.dark_mode = False
        self.panels: list[PumpPanel] = []
        self.vacuum_panel: Optional[VacuumPanel] = None
        self.recipes_panel: Optional[RecipesPanel] = None
        self._recipes_window: Optional[tk.Toplevel] = None
        self._sequence_editor: Optional[RecipeSequenceEditor] = None
        self._save_pump_labels_job: Optional[str] = None
        self._quick_recipes_cache: list[dict[str, Any]] = []
        self._quick_recipe_var = tk.StringVar(value="")
        self._recipe_abort_event = threading.Event()
        self._recipe_thread_running = False
        self._run_recipe_blink_after: Optional[str] = None
        self._quick_recipe_combo: Optional[ttk.Combobox] = None
        self._run_recipe_btn: Optional[tk.Button] = None
        self._abort_recipe_btn: Optional[ttk.Button] = None
        self._style = ttk.Style(self)
        self._style.theme_use("clam")
        self._build()
        self._schedule_live_updates()

    def _build(self) -> None:
        header = ttk.Frame(self, padding=12)
        header.pack(fill="x")
        ttk.Label(header, text="Mode").pack(side="left")
        ttk.Combobox(
            header,
            textvariable=self.mode_var,
            values=["Individual mode", "Dual mode", "Triple mode"],
            state="readonly",
            width=16,
        ).pack(side="left", padx=(6, 16))
        ttk.Checkbutton(header, text="Switch Together", variable=self.switch_together_var).pack(side="left")
        ttk.Button(header, text="Refresh COM Ports", command=self.refresh_ports).pack(side="left", padx=(16, 0))
        self.dark_mode_btn = ttk.Button(header, text="Dark Mode", command=self.toggle_dark_mode)
        self.dark_mode_btn.pack(side="left", padx=(16, 0))

        ttk.Button(header, text="Power Off (Stop All)", command=self.power_off).pack(side="right")
        ttk.Button(header, text="Recipes…", command=self.open_recipes_window).pack(side="right", padx=(0, 10))
        self._abort_recipe_btn = ttk.Button(header, text="Abort recipe", command=self.abort_recipe_run, state="disabled")
        self._abort_recipe_btn.pack(side="right", padx=(0, 6))
        theme0 = DARK if self.dark_mode else LIGHT
        self._run_recipe_btn = tk.Button(
            header,
            text="Run recipe",
            command=self._on_toolbar_run_recipe,
            bg=theme0["btn"],
            fg=theme0["fg"],
            activebackground=theme0["btn_hover"],
            padx=8,
            pady=2,
        )
        self._run_recipe_btn.pack(side="right", padx=(0, 6))
        self._quick_recipe_combo = ttk.Combobox(
            header,
            textvariable=self._quick_recipe_var,
            values=[],
            state="readonly",
            width=28,
        )
        self._quick_recipe_combo.pack(side="right", padx=(0, 6))
        ttk.Label(header, text="Recipe:").pack(side="right", padx=(16, 4))

        body = ttk.Frame(self, padding=(12, 0, 12, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        ports = detected_port_names()
        self.panels.append(PumpPanel(body, self, 1, ports))
        self.panels.append(PumpPanel(body, self, 2, ports))
        self.panels.append(PumpPanel(body, self, 3, ports))
        self.vacuum_panel = VacuumPanel(body, ports)

        self.panels[0].grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        self.panels[1].grid(row=0, column=1, sticky="nsew", pady=(0, 8))
        self.panels[2].grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        self.vacuum_panel.grid(row=1, column=1, sticky="nsew")

        pl = load_pump_labels_file()
        for i, panel in enumerate(self.panels):
            panel.nickname_var.set(pl.get(str(i + 1), ""))
            panel._refresh_frame_title()

        ttk.Label(
            self,
            text="Tip: you can type COM ports manually (e.g. COM3) even if no hardware is connected now.",
            padding=(12, 0, 12, 12),
        ).pack(anchor="w")

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_quick_recipe_combo()

    def refresh_quick_recipe_combo(self) -> None:
        """Reload saved recipes into the main toolbar drop-down."""
        recipes = load_stored_recipes()
        self._quick_recipes_cache = recipes
        labels = [recipe_display_name(r) for r in recipes]
        if self._quick_recipe_combo is not None:
            self._quick_recipe_combo.configure(values=labels)
        prev = self._quick_recipe_var.get().strip()
        if labels:
            if prev in labels:
                self._quick_recipe_var.set(prev)
            else:
                self._quick_recipe_var.set(labels[0])
        else:
            self._quick_recipe_var.set("")

    def _on_toolbar_run_recipe(self) -> None:
        if self._recipe_thread_running:
            messagebox.showwarning(
                "Recipe",
                "A recipe is already running. Use “Abort recipe” or wait until it finishes.",
                parent=self,
            )
            return
        recipes = self._quick_recipes_cache
        if not recipes:
            messagebox.showinfo(
                "Recipe",
                "No saved recipes yet. Open “Recipes…” and use “Save from main window”.",
                parent=self,
            )
            return
        idx = -1
        if self._quick_recipe_combo is not None:
            try:
                idx = int(self._quick_recipe_combo.current())
            except (tk.TclError, ValueError, TypeError):
                idx = -1
        sel = self._quick_recipe_var.get().strip()
        if (idx < 0 or idx >= len(recipes)) and sel:
            for i, r in enumerate(recipes):
                if recipe_display_name(r) == sel:
                    idx = i
                    break
        if idx < 0 or idx >= len(recipes):
            messagebox.showinfo("Recipe", "Select a recipe from the drop-down.", parent=self)
            return
        rec = recipes[idx]
        steps = rec.get("steps")
        if isinstance(steps, list) and len(steps) > 0:
            self.run_recipe_sequence(rec, from_toolbar=True)
        else:
            self.run_recipe(rec, from_toolbar=True)

    def abort_recipe_run(self) -> None:
        """Signal the background recipe worker to stop between steps (and during delays)."""
        self._recipe_abort_event.set()

    def _start_run_recipe_blink(self) -> None:
        self._stop_run_recipe_blink()
        blink_on = [False]

        def blink() -> None:
            if not self._recipe_thread_running:
                return
            blink_on[0] = not blink_on[0]
            if self._run_recipe_btn is not None and self._run_recipe_btn.winfo_exists():
                if blink_on[0]:
                    self._run_recipe_btn.configure(bg="#E02020", activebackground="#C01010", fg="white")
                else:
                    self._run_recipe_btn.configure(bg="#8B1515", activebackground="#6a1010", fg="white")
            self._run_recipe_blink_after = self.after(450, blink)

        blink()

    def _stop_run_recipe_blink(self) -> None:
        if self._run_recipe_blink_after is not None:
            try:
                self.after_cancel(self._run_recipe_blink_after)
            except tk.TclError:
                pass
            self._run_recipe_blink_after = None
        if self._run_recipe_btn is not None and self._run_recipe_btn.winfo_exists():
            theme = DARK if self.dark_mode else LIGHT
            self._run_recipe_btn.configure(bg=theme["btn"], fg=theme["fg"], activebackground=theme["btn_hover"])

    def _finish_recipe_run(self, from_toolbar: bool) -> None:
        self._recipe_thread_running = False
        self._recipe_abort_event.clear()
        if from_toolbar:
            self._stop_run_recipe_blink()
        if self._abort_recipe_btn is not None and self._abort_recipe_btn.winfo_exists():
            self._abort_recipe_btn.configure(state="disabled")

    def _begin_recipe_run(self, from_toolbar: bool) -> None:
        self._recipe_abort_event.clear()
        self._recipe_thread_running = True
        if self._abort_recipe_btn is not None:
            self._abort_recipe_btn.configure(state="normal")
        if from_toolbar:
            self._start_run_recipe_blink()

    def schedule_save_pump_labels(self) -> None:
        if self._save_pump_labels_job is not None:
            try:
                self.after_cancel(self._save_pump_labels_job)
            except tk.TclError:
                pass
        self._save_pump_labels_job = self.after(400, self._flush_pump_labels_to_file)

    def _flush_pump_labels_to_file(self) -> None:
        self._save_pump_labels_job = None
        save_pump_labels_file({str(i + 1): p.nickname_var.get().strip() for i, p in enumerate(self.panels)})

    def pump_nickname_map(self) -> dict[int, str]:
        return {
            i + 1: p.nickname_var.get().strip()
            for i, p in enumerate(self.panels)
            if p.nickname_var.get().strip()
        }

    def apply_recipe_to_panels(self, recipe: dict[str, Any]) -> None:
        """Copy saved COM/baud/address and pump1–pump3 parameter dicts into the main panels."""
        for i, panel in enumerate(self.panels):
            ckey = f"pump{i + 1}_conn"
            conn = recipe.get(ckey)
            if isinstance(conn, dict):
                com = str(conn.get("com", "")).strip()
                try:
                    baud = int(conn.get("baud", 19200))
                except (TypeError, ValueError):
                    baud = 19200
                try:
                    addr = int(conn.get("address", 0))
                except (TypeError, ValueError):
                    addr = 0
                panel.com_var.set(com)
                panel.baud_var.set(str(baud))
                panel.address_var.set(str(addr))
        for i, panel in enumerate(self.panels):
            key = f"pump{i + 1}"
            data = recipe.get(key)
            if isinstance(data, dict):
                panel.apply_settings_from_snapshot(data)
        pl = recipe.get("pump_labels")
        if isinstance(pl, dict):
            for i, panel in enumerate(self.panels):
                raw = pl.get(str(i + 1), pl.get(i + 1))
                if raw is not None and str(raw).strip():
                    panel.nickname_var.set(str(raw).strip())
                    panel._refresh_frame_title()
            self.schedule_save_pump_labels()

    def run_recipe_sequence(self, recipe: dict[str, Any], *, from_toolbar: bool = False) -> None:
        """Execute ordered steps (delays, pump connect/apply/run, vacuum) on a background thread."""
        steps = recipe.get("steps")
        if not isinstance(steps, list) or not steps:
            return
        if self._recipe_thread_running:
            messagebox.showwarning(
                "Recipe",
                "A recipe is already running. Abort it first or wait until it finishes.",
                parent=self,
            )
            return

        self._begin_recipe_run(from_toolbar)
        abort = self._recipe_abort_event
        app = self

        def worker() -> None:
            try:
                vac = app.vacuum_panel
                for idx, step in enumerate(steps):
                    if abort.is_set():
                        return
                    if not isinstance(step, dict):
                        continue
                    st = step.get("type")
                    try:
                        if st == "delay":
                            abortable_sleep(max(0.0, float(step.get("seconds", 0))), abort)
                            if abort.is_set():
                                return
                        elif st == "connect_pump":
                            p = int(step["pump"])
                            if p not in (1, 2, 3):
                                raise ValueError(f"Invalid pump {p}")
                            panel = app.panels[p - 1]
                            com = str(step["com"]).strip()
                            baud = int(step["baud"])
                            addr = int(step["address"])

                            def set_p_fields() -> None:
                                panel.com_var.set(com)
                                panel.baud_var.set(str(baud))
                                panel.address_var.set(str(addr))

                            if abort.is_set():
                                return
                            run_on_main_thread(app, set_p_fields)
                            if abort.is_set():
                                return
                            panel.connect_with_params(com, baud, addr)
                        elif st == "disconnect_pump":
                            p = int(step["pump"])
                            app.panels[p - 1].disconnect_sync()
                        elif st == "apply_pump":
                            p = int(step["pump"])
                            panel = app.panels[p - 1]
                            settings = step.get("settings")
                            if not isinstance(settings, dict):
                                raise ValueError("apply_pump needs a settings object")

                            def apply_u() -> None:
                                panel.apply_settings_from_snapshot(settings)

                            if abort.is_set():
                                return
                            run_on_main_thread(app, apply_u)
                            if abort.is_set():
                                return
                            panel.apply_settings_sync()
                        elif st == "run_pump":
                            p = int(step["pump"])
                            panel = app.panels[p - 1]
                            if abort.is_set():
                                return
                            panel.apply_settings_sync()
                            if abort.is_set():
                                return
                            panel.run_sync()
                        elif st == "stop_pump":
                            p = int(step["pump"])
                            app.panels[p - 1].stop_sync()
                        elif st == "vacuum_connect":
                            if vac is None:
                                raise RuntimeError("Vacuum panel not available")
                            com = str(step["com"]).strip()
                            baud = int(step.get("baud", 9600))

                            def set_v() -> None:
                                vac.com_var.set(com)

                            if abort.is_set():
                                return
                            run_on_main_thread(app, set_v)
                            if abort.is_set():
                                return
                            vac.open_serial_explicit(com, baud)
                            vac._wait_until_ready()
                        elif st == "vacuum_disconnect":
                            if vac is not None:
                                vac.close_serial_sync()
                        elif st == "vacuum_on":
                            if vac is None:
                                raise RuntimeError("Vacuum panel not available")
                            vac.send_vacuum_sync(True)
                        elif st == "vacuum_off":
                            if vac is None:
                                raise RuntimeError("Vacuum panel not available")
                            vac.send_vacuum_sync(False)
                        else:
                            raise ValueError(f"Unknown step type: {st}")
                    except Exception as exc:
                        msg = str(exc)
                        app.after(
                            0,
                            lambda m=msg, i=idx: messagebox.showerror(
                                "Recipe sequence",
                                f"Step {i + 1} failed:\n{m}",
                                parent=app,
                            ),
                        )
                        return
            finally:
                app.after(0, lambda ftb=from_toolbar: app._finish_recipe_run(ftb))

        threading.Thread(target=worker, daemon=True).start()

    def run_recipe(self, recipe: dict[str, Any], *, from_toolbar: bool = False) -> None:
        """Apply recipe to panel fields, then apply+run each pump that is connected."""
        if self._recipe_thread_running:
            messagebox.showwarning(
                "Recipe",
                "A recipe is already running. Abort it first or wait until it finishes.",
                parent=self,
            )
            return

        self._begin_recipe_run(from_toolbar)
        self.apply_recipe_to_panels(recipe)
        abort = self._recipe_abort_event
        app = self

        def work() -> None:
            try:
                for panel in app.panels:
                    if abort.is_set():
                        return
                    if panel.connection.pump is None:
                        panel.after(0, lambda p=panel: p.set_status("Recipe: not connected — skipped"))
                        continue
                    try:
                        panel.apply_settings_sync()
                        if abort.is_set():
                            return
                        panel.run_sync()
                        panel.after(0, lambda p=panel: p.set_status("Running (recipe)"))
                    except Exception as exc:
                        msg = str(exc)
                        panel.after(0, lambda p=panel, m=msg: p.set_status(f"Recipe error: {m}"))
            finally:
                app.after(0, lambda ftb=from_toolbar: app._finish_recipe_run(ftb))

        threading.Thread(target=work, daemon=True).start()

    def open_recipes_window(self) -> None:
        """Floating Recipes panel (child window, stays on top of main via transient)."""
        if self._recipes_window is not None:
            try:
                w = self._recipes_window
                if w.winfo_exists():
                    w.lift()
                    w.focus_force()
                    return
            except tk.TclError:
                self._recipes_window = None

        theme = DARK if self.dark_mode else LIGHT
        win = tk.Toplevel(self)
        win.title("Recipes — Syringe Pumps")
        win.transient(self)
        win.minsize(480, 580)
        win.geometry("600x620")
        win.configure(bg=theme["bg"])

        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)

        self.recipes_panel = RecipesPanel(outer, self)
        self.recipes_panel.pack(fill="both", expand=True)

        def on_recipes_close() -> None:
            self._recipes_window = None
            self.recipes_panel = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_recipes_close)
        self._recipes_window = win

    def _mode_target_indices(self, source_index: int) -> list[int]:
        mode = self.mode_var.get()
        if mode == "Dual mode":
            return [0, 1]
        if mode == "Triple mode":
            return [0, 1, 2]
        return [source_index]

    def _targets_for_action(self, source_panel: PumpPanel) -> list[PumpPanel]:
        source_idx = source_panel.pump_index - 1
        if not self.switch_together_var.get():
            return [source_panel]
        return [self.panels[i] for i in self._mode_target_indices(source_idx)]

    def _run_group_action(self, source_panel: PumpPanel, action_name: str) -> None:
        targets = self._targets_for_action(source_panel)
        snapshot = source_panel.snapshot_settings()

        def work() -> None:
            for panel in targets:
                if panel is not source_panel:
                    panel.after(0, lambda p=panel: p.apply_settings_from_snapshot(snapshot))
                if action_name == "apply":
                    panel.apply_settings_sync()
                    panel.after(0, lambda p=panel: p.set_status("Settings applied"))
                elif action_name == "run":
                    panel.apply_settings_sync()
                    panel.run_sync()
                    panel.after(0, lambda p=panel: p.set_status("Running"))
                elif action_name == "stop":
                    panel.stop_sync()
                    panel.after(0, lambda p=panel: p.set_status("Stopped"))

        threading.Thread(target=self._guarded_work, args=(work, targets), daemon=True).start()

    @staticmethod
    def _guarded_work(work, targets: list[PumpPanel]) -> None:
        try:
            work()
        except Exception as exc:
            msg = str(exc)
            for panel in targets:
                panel.after(0, lambda p=panel, m=msg: p.set_status(f"Error: {m}"))

    def apply_with_mode(self, source_panel: PumpPanel) -> None:
        self._run_group_action(source_panel, "apply")

    def run_with_mode(self, source_panel: PumpPanel) -> None:
        self._run_group_action(source_panel, "run")

    def stop_with_mode(self, source_panel: PumpPanel) -> None:
        self._run_group_action(source_panel, "stop")

    def refresh_ports(self) -> None:
        ports = detected_port_names()
        for panel in self.panels:
            panel.update_port_choices(ports)
        if self.vacuum_panel is not None:
            self.vacuum_panel.update_port_choices(ports)
        if ports:
            messagebox.showinfo("COM Ports", "Detected: " + ", ".join(ports))
        else:
            messagebox.showinfo("COM Ports", "No COM ports detected. Manual entry is still available.")

    def power_off(self) -> None:
        def work() -> None:
            for panel in self.panels:
                if panel.connection.pump is not None:
                    try:
                        panel.stop_sync()
                    except Exception:
                        pass
                    panel.after(0, lambda p=panel: p.set_status("Powered off / stopped"))
            if self.vacuum_panel is not None:
                self.vacuum_panel.force_off()

        threading.Thread(target=work, daemon=True).start()

    def toggle_dark_mode(self) -> None:
        self.dark_mode = not self.dark_mode
        theme = DARK if self.dark_mode else LIGHT
        apply_theme(self._style, theme)
        self.configure(bg=theme["bg"])
        self.dark_mode_btn.configure(text="Light Mode" if self.dark_mode else "Dark Mode")
        if self._run_recipe_btn is not None and self._run_recipe_btn.winfo_exists() and not self._recipe_thread_running:
            self._run_recipe_btn.configure(
                bg=theme["btn"],
                fg=theme["fg"],
                activebackground=theme["btn_hover"],
            )
        if self._recipes_window is not None:
            try:
                if self._recipes_window.winfo_exists():
                    self._recipes_window.configure(bg=theme["bg"])
            except tk.TclError:
                pass
        if self.recipes_panel is not None:
            self.recipes_panel.on_theme_changed()
        if self._sequence_editor is not None:
            try:
                if self._sequence_editor.winfo_exists():
                    self._sequence_editor.on_parent_theme_changed()
            except tk.TclError:
                self._sequence_editor = None

    def _schedule_live_updates(self) -> None:
        for panel in self.panels:
            panel.update_time_display()
            panel.maybe_poll_live_status()
        self.after(1000, self._schedule_live_updates)

    def on_close(self) -> None:
        self._recipe_abort_event.set()
        for panel in self.panels:
            panel.connection.close()
        if self.vacuum_panel is not None:
            self.vacuum_panel.close()
        if self._sequence_editor is not None:
            try:
                self._sequence_editor.destroy()
            except tk.TclError:
                pass
            self._sequence_editor = None
        if self._recipes_window is not None:
            try:
                self._recipes_window.destroy()
            except tk.TclError:
                pass
            self._recipes_window = None
            self.recipes_panel = None
        self.destroy()


def main() -> None:
    app = PumpControllerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
