"""
Tkinter GUI for controlling three New Era NE-1000 syringe pumps and an Arduino vacuum.
"""

from __future__ import annotations

import time
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk
from typing import Optional

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
STX = 0x02
ETX = 0x03


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
        self.connection = PumpConnection()
        self.run_start_ts: Optional[float] = None
        self._polling = False
        self._build(ports)

    def _build(self, ports: list[str]) -> None:
        ttk.Label(self, text="COM Port").grid(row=0, column=0, sticky="w")
        self.com_var = tk.StringVar(value=(ports[0] if ports else ""))
        self.com_combo = ttk.Combobox(self, textvariable=self.com_var, values=ports, width=12)
        self.com_combo.grid(row=0, column=1, sticky="w", padx=(6, 10))

        ttk.Label(self, text="Address").grid(row=0, column=2, sticky="w")
        self.address_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.address_var, width=6).grid(row=0, column=3, sticky="w", padx=(6, 0))

        ttk.Label(self, text="Baud Rate").grid(row=0, column=4, sticky="w", padx=(10, 0))
        # NE-1000 pumps often ship at 19200; NESP-Lib Port defaults to 9600 — pick what matches your pump menu.
        self.baud_var = tk.StringVar(value="19200")
        ttk.Combobox(self, textvariable=self.baud_var, values=BAUD_RATES, state="readonly", width=8).grid(
            row=0, column=5, sticky="w", padx=(6, 0))

        ttk.Label(self, text="Syringe").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.syringe_var = tk.StringVar(value="BD 10 mL (10 cc)")
        self.syringe_combo = ttk.Combobox(
            self,
            textvariable=self.syringe_var,
            values=list(SYRINGE_PRESETS_MM.keys()),
            state="readonly",
            width=18,
        )
        self.syringe_combo.grid(row=1, column=1, sticky="w", padx=(6, 10), pady=(6, 0))
        self.syringe_combo.bind("<<ComboboxSelected>>", lambda _: self._on_syringe_preset_change())

        ttk.Label(self, text="Custom Diameter (mm)").grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.custom_diameter_var = tk.StringVar(value="14.50")
        ttk.Entry(self, textvariable=self.custom_diameter_var, width=10).grid(row=1, column=3, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self, text="Rate Units").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.rate_units_var = tk.StringVar(value="mL/min")
        ttk.Combobox(self, textvariable=self.rate_units_var, values=RATE_UNITS, state="readonly", width=12).grid(
            row=2, column=1, sticky="w", padx=(6, 10), pady=(6, 0)
        )

        ttk.Label(self, text="Pumping Rate").grid(row=2, column=2, sticky="w", pady=(6, 0))
        self.rate_var = tk.StringVar(value="0.1")
        ttk.Entry(self, textvariable=self.rate_var, width=10).grid(row=2, column=3, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self, text="Dispense Mode").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.dispense_mode_var = tk.StringVar(value="Continuous")
        ttk.Combobox(self, textvariable=self.dispense_mode_var, values=DISPENSE_MODES, state="readonly", width=12).grid(
            row=3, column=1, sticky="w", padx=(6, 10), pady=(6, 0)
        )

        ttk.Label(self, text="Volume to Dispense (uL)").grid(row=3, column=2, sticky="w", pady=(6, 0))
        self.volume_ul_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.volume_ul_var, width=10).grid(row=3, column=3, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self, text="Direction").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.direction_var = tk.StringVar(value="Infuse")
        ttk.Combobox(self, textvariable=self.direction_var, values=["Infuse", "Withdraw"], state="readonly", width=12).grid(
            row=4, column=1, sticky="w", padx=(6, 10), pady=(6, 0)
        )

        ttk.Label(self, text="Volume Dispensed (uL)").grid(row=4, column=2, sticky="w", pady=(6, 0))
        self.dispensed_ul_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.dispensed_ul_var, width=10, state="readonly").grid(
            row=4, column=3, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        ttk.Label(self, text="Time (sec)").grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.time_sec_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.time_sec_var, width=10, state="readonly").grid(
            row=5, column=1, sticky="w", padx=(6, 10), pady=(6, 0)
        )

        ttk.Label(self, text="Total Volume Dispensed (uL)").grid(row=5, column=2, sticky="w", pady=(6, 0))
        self.total_dispensed_ul_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.total_dispensed_ul_var, width=10, state="readonly").grid(
            row=5, column=3, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        btn_row = ttk.Frame(self)
        btn_row.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(10, 0))
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
        btn_row2.grid(row=7, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(btn_row2, text="Pump Auto-Connect", command=self.auto_connect_pump).pack(side="left", padx=4)

        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(self, textvariable=self.status_var).grid(row=8, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.columnconfigure(1, weight=1)
        self._on_syringe_preset_change()

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

    def disconnect(self) -> None:
        def work() -> None:
            self.connection.close()
            self.run_start_ts = None
            self.after(0, lambda: self.set_status("Disconnected"))

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
        if not com_name:
            raise ValueError("Select or type a COM port for the vacuum Arduino.")
        if self.serial_conn is not None and self.connected_com == com_name and self.serial_conn.is_open:
            return self.serial_conn

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

        ttk.Label(
            self,
            text="Tip: you can type COM ports manually (e.g. COM3) even if no hardware is connected now.",
            padding=(12, 0, 12, 12),
        ).pack(anchor="w")

        self.protocol("WM_DELETE_WINDOW", self.on_close)

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

    def _schedule_live_updates(self) -> None:
        for panel in self.panels:
            panel.update_time_display()
            panel.maybe_poll_live_status()
        self.after(1000, self._schedule_live_updates)

    def on_close(self) -> None:
        for panel in self.panels:
            panel.connection.close()
        if self.vacuum_panel is not None:
            self.vacuum_panel.close()
        self.destroy()


def main() -> None:
    app = PumpControllerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
