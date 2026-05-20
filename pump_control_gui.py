"""
Tkinter GUI for controlling up to ten New Era NE-1000 syringe pumps, an Arduino-driven
vacuum + MPX5100DP pressure sensor, and a RUNZE SV-07 multiport selector valve.
"""

from __future__ import annotations

import copy
import json
import queue
import re
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

from sv07_driver import SV07, SV07Error


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

BTN_POWER_OFF_ABORT = {
    "bg": "#B91C1C",
    "fg": "#FFEB3B",
    "activebackground": "#7F1D1D",
    "activeforeground": "#FFEB3B",
    "font": ("Segoe UI", 9, "bold"),
}
BTN_APPLY_ACCENT = {
    "bg": "#F5C518",
    "fg": "#1a1a1a",
    "activebackground": "#E0AF00",
    "activeforeground": "#000000",
    "font": ("Segoe UI", 9, "bold"),
}


def _resolve_color_to_hex(widget: tk.Widget, color: str) -> str:
    """Convert any Tk colour name/system colour to a '#RRGGBB' hex string."""
    if color.startswith("#") and len(color) == 7:
        return color
    try:
        rgb = widget.winfo_rgb(color)
        return f"#{rgb[0] >> 8:02X}{rgb[1] >> 8:02X}{rgb[2] >> 8:02X}"
    except Exception:
        return "#D0D0D0"


def _brighten_hex(color: str, amount: int = 35) -> str:
    """Lighten a hex colour by *amount* (0-255) per channel, clamping at 255."""
    color = color.lstrip("#")
    r, g, b = int(color[:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    r = min(255, r + amount)
    g = min(255, g + amount)
    b = min(255, b + amount)
    return f"#{r:02X}{g:02X}{b:02X}"


def add_hover_glow(btn: tk.Button, normal_bg: Optional[str] = None) -> None:
    """Bind <Enter>/<Leave> so the button brightens on hover, like ttk buttons."""
    base = _resolve_color_to_hex(btn, normal_bg or btn.cget("bg"))
    hover = _brighten_hex(base, 40)
    btn.bind("<Enter>", lambda _e: btn.configure(bg=hover), add=True)
    btn.bind("<Leave>", lambda _e: btn.configure(bg=base), add=True)


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
VALVE_BAUD_RATES = ["9600", "19200", "38400", "57600", "115200"]
VALVE_PORT_COUNT_OPTIONS = [6, 8, 10, 12, 16]
STX = 0x02
ETX = 0x03

MIN_PUMPS = 1
MAX_PUMPS = 10
DEFAULT_NUM_PUMPS = 3

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


def _clamp_pump_count(n: Any) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return DEFAULT_NUM_PUMPS
    return max(MIN_PUMPS, min(MAX_PUMPS, v))


def _coerce_pump_port_map(raw: Any) -> dict[int, int]:
    """Coerce a raw mapping into ``{pump_index: valve_port}`` with bounds-checked keys."""
    out: dict[int, int] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            idx = int(k)
            port = int(v)
        except (TypeError, ValueError):
            continue
        if MIN_PUMPS <= idx <= MAX_PUMPS and 1 <= port <= 16:
            out[idx] = port
    return out


def _coerce_port_labels(raw: Any) -> dict[int, str]:
    """Coerce a raw mapping into ``{valve_port: label}`` (e.g. ``{5: "Vent"}``).

    Used for non-pump inlets like vents, bleeds, waste, atmosphere, etc.
    """
    out: dict[int, str] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            port = int(k)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 16 and v is not None:
            text = str(v).strip()
            if text:
                out[port] = text
    return out


def load_pump_labels_file() -> tuple[int, dict[str, str], dict[int, int], dict[int, str]]:
    """
    Return ``(num_pumps, labels, pump_port_map, port_labels)`` from ``pump_labels.json``.

    Supports both the new schema
    (``{"num_pumps": N, "pumps": {"1": "name", ...},
        "pump_port_map": {"1": 3, ...}, "port_labels": {"5": "Vent", ...}}``)
    and the legacy schema (``{"1": "name", "2": "name", "3": "name"}``).
    """
    path = pump_labels_file_path()
    if not path.is_file():
        return DEFAULT_NUM_PUMPS, {}, {}, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_NUM_PUMPS, {}, {}, {}
    if not isinstance(data, dict):
        return DEFAULT_NUM_PUMPS, {}, {}, {}

    port_map = _coerce_pump_port_map(data.get("pump_port_map"))
    port_labels = _coerce_port_labels(data.get("port_labels"))

    pumps_block = data.get("pumps")
    if isinstance(pumps_block, dict):
        num = _clamp_pump_count(data.get("num_pumps", DEFAULT_NUM_PUMPS))
        labels: dict[str, str] = {}
        for k, v in pumps_block.items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            if MIN_PUMPS <= idx <= MAX_PUMPS and v is not None:
                labels[str(idx)] = str(v).strip()
        return num, labels, port_map, port_labels

    legacy_labels: dict[str, str] = {}
    for k in (str(i) for i in range(MIN_PUMPS, MAX_PUMPS + 1)):
        if k in data and data[k] is not None:
            legacy_labels[k] = str(data[k]).strip()
    if not legacy_labels:
        return DEFAULT_NUM_PUMPS, {}, port_map, port_labels
    inferred = max((int(k) for k in legacy_labels), default=DEFAULT_NUM_PUMPS)
    return (
        _clamp_pump_count(max(inferred, DEFAULT_NUM_PUMPS)),
        legacy_labels,
        port_map,
        port_labels,
    )


def save_pump_labels_file(
    num_pumps: int,
    labels: dict[str, str],
    pump_port_map: Optional[dict[int, int]] = None,
    port_labels: Optional[dict[int, str]] = None,
) -> None:
    n = _clamp_pump_count(num_pumps)
    payload: dict[str, Any] = {
        "num_pumps": n,
        "pumps": {str(k): (labels.get(str(k), "") or "").strip() for k in range(1, n + 1)},
    }
    pm = _coerce_pump_port_map(pump_port_map or {})
    if pm:
        payload["pump_port_map"] = {str(k): pm[k] for k in sorted(pm)}
    pl = _coerce_port_labels(port_labels or {})
    if pl:
        payload["port_labels"] = {str(k): pl[k] for k in sorted(pl)}
    pump_labels_file_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")


def recipe_num_pumps(recipe: dict[str, Any]) -> int:
    """Infer the pump count for a recipe (new ``num_pumps`` field, else legacy keys)."""
    if "num_pumps" in recipe:
        return _clamp_pump_count(recipe.get("num_pumps"))
    pumps_map = recipe.get("pumps")
    if isinstance(pumps_map, dict):
        keys = []
        for k in pumps_map.keys():
            try:
                keys.append(int(k))
            except (TypeError, ValueError):
                continue
        if keys:
            return _clamp_pump_count(max(keys))
    legacy_max = 0
    for i in range(1, MAX_PUMPS + 1):
        if isinstance(recipe.get(f"pump{i}"), dict):
            legacy_max = max(legacy_max, i)
    if legacy_max > 0:
        return _clamp_pump_count(legacy_max)
    return DEFAULT_NUM_PUMPS


def recipe_pump_settings(recipe: dict[str, Any], pump_index: int) -> Optional[dict[str, Any]]:
    """Get a pump-settings dict for ``pump_index`` (1-based) supporting both schemas."""
    if pump_index < MIN_PUMPS or pump_index > MAX_PUMPS:
        return None
    pumps_map = recipe.get("pumps")
    if isinstance(pumps_map, dict):
        block = pumps_map.get(str(pump_index)) or pumps_map.get(pump_index)
        if isinstance(block, dict):
            return block
    legacy = recipe.get(f"pump{pump_index}")
    if isinstance(legacy, dict):
        return legacy
    return None


def recipe_pump_conn(recipe: dict[str, Any], pump_index: int) -> Optional[dict[str, Any]]:
    if pump_index < MIN_PUMPS or pump_index > MAX_PUMPS:
        return None
    conns_map = recipe.get("pump_conns")
    if isinstance(conns_map, dict):
        block = conns_map.get(str(pump_index)) or conns_map.get(pump_index)
        if isinstance(block, dict):
            return block
    legacy = recipe.get(f"pump{pump_index}_conn")
    if isinstance(legacy, dict):
        return legacy
    return None


def recipe_valve_conn(recipe: dict[str, Any]) -> Optional[dict[str, Any]]:
    block = recipe.get("valve_conn")
    return block if isinstance(block, dict) else None


def recipe_pump_port_map(recipe: dict[str, Any]) -> dict[int, int]:
    """Return ``{pump_index: valve_port}`` from ``pump_port_map`` if present."""
    raw = recipe.get("pump_port_map")
    if not isinstance(raw, dict):
        return {}
    out: dict[int, int] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


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

    _tk_safe_schedule(app, wrapper)
    if not event.wait(timeout=timeout):
        raise TimeoutError("GUI thread timed out")
    if err:
        raise err[0]
    return result[0] if result else None


def _tk_safe_schedule(app: tk.Tk, fn: Callable[[], None]) -> None:
    """Run *fn* on the Tk main thread (required for thread-safe GUI updates)."""
    sched = getattr(app, "schedule_gui", None)
    if callable(sched):
        sched(fn)
    else:
        app.after(0, fn)


class RecipeAbort(Exception):
    """Raised when the recipe worker must stop (user aborted during a GUI wait)."""


def run_on_main_thread_abortable(
    app: tk.Tk,
    fn: Callable[[], Any],
    abort: threading.Event,
    timeout: float = 120.0,
) -> Any:
    """Like ``run_on_main_thread`` but wakes periodically so *abort* can interrupt the wait."""
    if abort.is_set():
        raise RecipeAbort()
    result: list[Any] = []
    err: list[BaseException] = []
    event = threading.Event()

    def wrapper() -> None:
        try:
            if abort.is_set():
                return
            result.append(fn())
        except BaseException as exc:
            err.append(exc)
        finally:
            event.set()

    _tk_safe_schedule(app, wrapper)
    end = time.monotonic() + timeout
    while not event.is_set():
        if abort.is_set():
            raise RecipeAbort()
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("GUI thread timed out")
        event.wait(min(0.15, remaining))
    if err:
        raise err[0]
    if abort.is_set():
        raise RecipeAbort()
    if not result:
        return None
    return result[0]


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


def recipe_confirm_pump_line_open(
    app: tk.Tk,
    pump_index: int,
    nicknames: dict[int, str],
    abort: threading.Event,
) -> bool:
    """
    Show a modal checkpoint on the main window **before** a pump run starts. The recipe worker
    polls *abort* so **Abort recipe** works while the dialog is open. Returns False to stop the recipe.
    """
    nick = nicknames.get(pump_index) or f"Pump {pump_index}"
    outcome: list[Optional[bool]] = [None]
    dialog_holder: list[Optional[tk.Toplevel]] = [None]
    t_open: list[Optional[float]] = [None]

    def add_pause_time() -> None:
        if t_open[0] is None:
            return
        dt = time.time() - t_open[0]
        if hasattr(app, "_recipe_progress_pause_accum"):
            app._recipe_progress_pause_accum += dt
        t_open[0] = None

    def refresh_progress() -> None:
        fn = getattr(app, "_refresh_recipe_progress_after_checkpoint", None)
        if callable(fn):
            fn()

    def build() -> None:
        pf = getattr(app, "_progress_frame", None)
        pl = getattr(app, "_progress_label", None)
        if pf is not None and pl is not None:
            try:
                if pf.winfo_ismapped():
                    pl.configure(
                        text=f"Recipe paused — confirm Pump {pump_index} line is open, then tap Continue.",
                    )
            except tk.TclError:
                pass
        top = tk.Toplevel(app)
        dialog_holder[0] = top
        top.title("Line check")
        top.transient(app)
        top.resizable(False, False)
        try:
            dark = bool(getattr(app, "dark_mode", False))
        except Exception:
            dark = False
        theme = DARK if dark else LIGHT
        top.configure(bg=theme["bg"])
        frm = tk.Frame(top, bg=theme["bg"], padx=18, pady=16)
        frm.pack(fill="both", expand=True)
        msg = (
            f"Confirm pump {pump_index} line is open ({nick}).\n\n"
            "Tap Continue when the line is ready."
        )
        tk.Label(
            frm,
            text=msg,
            bg=theme["bg"],
            fg=theme["fg"],
            justify="left",
            wraplength=420,
        ).pack(anchor="w", pady=(0, 14))
        btns = tk.Frame(frm, bg=theme["bg"])
        btns.pack(fill="x")

        def on_continue() -> None:
            add_pause_time()
            outcome[0] = True
            try:
                top.grab_release()
            except tk.TclError:
                pass
            top.destroy()
            dialog_holder[0] = None
            refresh_progress()

        def on_abort_click() -> None:
            add_pause_time()
            abort.set()
            outcome[0] = False
            try:
                top.grab_release()
            except tk.TclError:
                pass
            top.destroy()
            dialog_holder[0] = None
            refresh_progress()

        top.protocol("WM_DELETE_WINDOW", on_abort_click)
        tk.Button(
            btns,
            text="Continue",
            command=on_continue,
            padx=14,
            pady=6,
            cursor="hand2",
        ).pack(side="left", padx=(0, 8))
        tk.Button(
            btns,
            text="Abort recipe",
            command=on_abort_click,
            padx=14,
            pady=6,
            cursor="hand2",
        ).pack(side="left")
        top.update_idletasks()
        t_open[0] = time.time()
        top.grab_set()
        top.focus_force()
        top.lift()

    _tk_safe_schedule(app, build)
    while outcome[0] is None:
        if abort.is_set():
            outcome[0] = False

            def destroy_dialog() -> None:
                d = dialog_holder[0]
                if d is None:
                    return
                try:
                    d.grab_release()
                except tk.TclError:
                    pass
                try:
                    if d.winfo_exists():
                        d.destroy()
                except tk.TclError:
                    pass
                dialog_holder[0] = None

            _tk_safe_schedule(app, destroy_dialog)
            if t_open[0] is not None and hasattr(app, "_recipe_progress_pause_accum"):
                app._recipe_progress_pause_accum += time.time() - t_open[0]
                t_open[0] = None
            _tk_safe_schedule(app, refresh_progress)
            return False
        time.sleep(0.05)
    if outcome[0] is not True:
        return False
    return not abort.is_set()


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
    if t == "line_check":
        return f"Confirm line open — {_pump_step_name(step.get('pump'), nicknames)}"
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
    if t == "valve_connect":
        b = step.get("baud", 9600)
        addr = step.get("address", 0)
        mp = step.get("max_ports")
        mp_s = f" max_ports {mp}" if mp is not None else ""
        return f"Valve connect {step.get('com')} @ {b} addr {addr}{mp_s}"
    if t == "valve_disconnect":
        return "Valve disconnect"
    if t == "valve_to_port":
        return f"Valve → port {step.get('port', '?')}"
    if t == "valve_to_pump":
        return f"Valve → line for {_pump_step_name(step.get('pump'), nicknames)}"
    if t == "valve_to_label":
        lab = str(step.get("label", "?")).strip() or "?"
        return f"Valve → label '{lab}'"
    if t == "disconnect_everything":
        return "Disconnect all (stop pumps, vacuum off, serial closed)"
    return str(step)


def format_step_list_line(index: int, step: dict[str, Any], nicknames: Optional[dict[int, str]] = None) -> str:
    body = format_step_summary(step, nicknames)
    lab = (step.get("label") or step.get("step_label") or "").strip()
    if lab:
        return f"{index + 1}. [{lab}] {body}"
    return f"{index + 1}. {body}"


def _estimate_step_seconds(step: dict[str, Any]) -> float:
    """Best-effort estimate of how long a single recipe step takes (seconds)."""
    t = step.get("type", "")
    if t == "delay":
        return max(0.0, float(step.get("seconds", 0)))
    if t == "apply_pump":
        s = step.get("settings") if isinstance(step.get("settings"), dict) else {}
        if s.get("dispense_mode") == "Volume":
            try:
                vol_ul = float(s.get("volume_ul", 0))
                rate_val = float(s.get("rate_value", 0))
                rate_units = s.get("rate_units", "mL/min")
                ml_per_min = to_ml_per_min(rate_val, rate_units)
                if ml_per_min > 0 and vol_ul > 0:
                    vol_ml = vol_ul / 1000.0
                    return (vol_ml / ml_per_min) * 60.0
            except (ValueError, ZeroDivisionError):
                pass
    if t in ("valve_to_port", "valve_to_pump", "valve_to_label"):
        # SV-07 max travel time is ~2 s; budget a small buffer for status polling.
        return 3.0
    return 0.0


def _estimate_recipe_time(recipe: dict[str, Any]) -> float:
    """Return total estimated seconds for a recipe (sequence or simple)."""
    steps = recipe.get("steps")
    if isinstance(steps, list) and steps:
        return sum(_estimate_step_seconds(s) for s in steps if isinstance(s, dict))
    return 0.0


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "< 1 s (instant / unknown)"
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min {s:02d} s"


def _recipe_resources(recipe: dict[str, Any]) -> tuple[set[int], bool, bool]:
    """Return (pumps used, vacuum needed, valve needed) from recipe steps."""
    pumps_used: set[int] = set()
    vacuum_needed = False
    valve_needed = False
    steps = recipe.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            t = step.get("type", "")
            if t in ("connect_pump", "disconnect_pump", "apply_pump", "run_pump", "stop_pump", "line_check"):
                try:
                    pumps_used.add(int(step["pump"]))
                except (KeyError, ValueError, TypeError):
                    pass
            elif t in ("vacuum_connect", "vacuum_disconnect", "vacuum_on", "vacuum_off"):
                vacuum_needed = True
            elif t in (
                "valve_connect",
                "valve_disconnect",
                "valve_to_port",
                "valve_to_pump",
                "valve_to_label",
            ):
                valve_needed = True
                if t == "valve_to_pump":
                    try:
                        pumps_used.add(int(step["pump"]))
                    except (KeyError, ValueError, TypeError):
                        pass
            elif t == "disconnect_everything":
                vacuum_needed = True
                valve_needed = True
                for i in range(1, recipe_num_pumps(recipe) + 1):
                    pumps_used.add(i)
    else:
        for i in range(1, MAX_PUMPS + 1):
            if recipe_pump_settings(recipe, i) is not None:
                pumps_used.add(i)
    return pumps_used, vacuum_needed, valve_needed


def _friendly_step_error(step: dict[str, Any], step_index: int, error_msg: str, nicknames: Optional[dict[int, str]] = None) -> str:
    """Build a user-friendly error message with a suggestion for a failed recipe step."""
    t = step.get("type", "?")
    step_desc = format_step_summary(step, nicknames)
    lines = [
        f"Step {step_index + 1} failed: {step_desc}",
        "",
        f"Error: {error_msg}",
        "",
        "Suggestion:",
    ]
    low = error_msg.lower()
    if t in ("connect_pump", "apply_pump", "run_pump", "stop_pump", "line_check", "disconnect_pump"):
        pnum = step.get("pump", "?")
        if "could not open port" in low or "denied" in low or "filenotfounderror" in low:
            lines.append(f"  - Check that pump {pnum}'s USB cable is plugged in and the COM port is correct.")
            lines.append(f"  - Make sure no other program is using the port.")
        elif "not connected" in low or "no pump" in low or "nonetype" in low:
            lines.append(f"  - Pump {pnum} is not connected. Add a 'Connect pump' step before this step,")
            lines.append(f"    or manually connect pump {pnum} before running the recipe.")
        elif "timeout" in low:
            lines.append(f"  - Pump {pnum} did not respond. Check power, baud rate, and address settings.")
        else:
            lines.append(f"  - Verify pump {pnum} is powered on, connected, and settings are correct.")
    elif t in ("vacuum_connect", "vacuum_on", "vacuum_off"):
        if "could not open port" in low or "denied" in low or "filenotfounderror" in low:
            lines.append("  - Check the Arduino USB cable and COM port assignment.")
            lines.append("  - Make sure no other program is using the port.")
        elif "not available" in low or "not connected" in low:
            lines.append("  - The vacuum Arduino is not connected. Add a 'Vacuum connect' step,")
            lines.append("    or connect it manually before running.")
        else:
            lines.append("  - Verify the Arduino is powered and the COM port / baud rate are correct.")
    elif t in (
        "valve_connect",
        "valve_disconnect",
        "valve_to_port",
        "valve_to_pump",
        "valve_to_label",
    ):
        if "could not open port" in low or "denied" in low or "filenotfounderror" in low:
            lines.append("  - Check the SV-07 USB-to-RS232/RS485 adapter and COM port assignment.")
            lines.append("  - Make sure no other program is using the port.")
        elif "not connected" in low or "not open" in low:
            lines.append("  - The selector valve is not connected. Add a 'Valve connect' step,")
            lines.append("    or connect it manually from the Selector Valve tab before running.")
        elif "no pump-to-port mapping" in low:
            lines.append("  - Open the recipe and assign a valve port for this pump in the")
            lines.append("    pump-to-port mapping section.")
        elif "no port labelled" in low or "no port labeled" in low:
            lines.append("  - Open the Selector Valve tab and add a Custom port label that")
            lines.append("    matches this step's label (case-insensitive, trimmed).")
        elif "checksum" in low or "bad response" in low or "incomplete response" in low:
            lines.append("  - The valve replied with a malformed frame. Double-check baud rate")
            lines.append("    and address; verify wiring (RS232 TX/RX or RS485 A/B not swapped).")
        elif "timeout" in low or "did not complete" in low:
            lines.append("  - Valve did not finish moving. Check 24 V power and that the rotor")
            lines.append("    is not jammed; raise the move timeout in the valve panel if needed.")
        else:
            lines.append("  - Verify the SV-07 is powered (24 V) and the COM/baud/address match.")
    elif t == "disconnect_everything":
        lines.append("  - If a port refused to close, disconnect it manually from each device tab.")
    elif t == "delay":
        lines.append("  - Check that the delay value is a valid number of seconds.")
    else:
        lines.append("  - Review this step's settings in the sequence editor.")
    return "\n".join(lines)


_POPUP_ICONS = {
    "info": "\u2139\uFE0F",
    "success": "\u2705",
    "warning": "\u26A0\uFE0F",
    "error": "\u274C",
}
_POPUP_ACCENT = {
    "info": "#3B82F6",
    "success": "#28A745",
    "warning": "#E67E22",
    "error": "#DC3545",
}


def show_popup(
    parent: tk.Widget,
    title: str,
    message: str,
    level: str = "info",
    detail: str = "",
    dark_mode: bool = False,
) -> None:
    """Show a themed, modern popup dialog."""
    theme = DARK if dark_mode else LIGHT
    accent = _POPUP_ACCENT.get(level, _POPUP_ACCENT["info"])
    icon_char = _POPUP_ICONS.get(level, _POPUP_ICONS["info"])

    d = tk.Toplevel(parent)
    d.title(title)
    d.transient(parent)
    d.grab_set()
    d.resizable(False, False)
    d.configure(bg=theme["bg"])

    outer = ttk.Frame(d, padding=20)
    outer.pack(fill="both", expand=True)

    hdr = ttk.Frame(outer)
    hdr.pack(fill="x", pady=(0, 10))
    tk.Label(
        hdr, text=icon_char, font=("Segoe UI", 22), bg=theme["bg"],
    ).pack(side="left", padx=(0, 10))
    ttk.Label(hdr, text=title, font=("Segoe UI", 13, "bold"), foreground=accent).pack(side="left", anchor="s")

    ttk.Label(
        outer, text=message, font=("Segoe UI", 10), wraplength=460, justify="left",
    ).pack(anchor="w", pady=(0, 4))

    if detail:
        det_fr = ttk.Frame(outer)
        det_fr.pack(fill="both", expand=True, pady=(6, 8))
        det_text = tk.Text(
            det_fr, wrap="word", width=60, height=min(14, max(4, detail.count("\n") + 2)),
            bg=theme["input"], fg=theme["fg"], font=("Consolas", 9),
            relief="flat", bd=0, highlightthickness=0,
        )
        det_text.pack(fill="both", expand=True)
        det_text.insert("1.0", detail)
        det_text.configure(state="disabled")

    ok_btn = tk.Button(
        outer, text="OK", command=d.destroy,
        bg=accent, fg="white", activebackground=accent, activeforeground="white",
        font=("Segoe UI", 10, "bold"), padx=24, pady=4, cursor="hand2",
        relief="flat", bd=0,
    )
    ok_btn.pack(anchor="e", pady=(8, 0))
    add_hover_glow(ok_btn, accent)

    d.update_idletasks()
    w = max(d.winfo_reqwidth(), 380)
    h = d.winfo_reqheight()
    px = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
    py = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
    d.geometry(f"{w}x{h}+{px}+{py}")
    d.wait_window()


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


VOLUME_DISPLAY_UNITS = ("uL", "mL", "L")


def format_volume_ul_for_display(ul: float, unit: str) -> str:
    """Format a volume given in microliters for display in uL, mL, or L."""
    if not ul:
        return "0"
    if unit == "mL":
        v = ul / 1000.0
        s = f"{v:.6f}".rstrip("0").rstrip(".")
        return s if s else "0"
    if unit == "L":
        v = ul / 1_000_000.0
        s = f"{v:.9f}".rstrip("0").rstrip(".")
        return s if s else "0"
    return f"{ul:.1f}"


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
        self.live_readout_var = tk.StringVar(value="\u2014")
        self._volume_disp_active_ul: float = 0.0
        self._volume_disp_total_ul: float = 0.0
        self._last_poll_err: Optional[str] = None
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

    def _current_volume_display_unit(self) -> str:
        u = self.volume_display_unit_var.get().strip() if hasattr(self, "volume_display_unit_var") else "uL"
        return u if u in VOLUME_DISPLAY_UNITS else "uL"

    def _refresh_dispensed_volume_display(self) -> None:
        u = self._current_volume_display_unit()
        self.dispensed_ul_var.set(format_volume_ul_for_display(self._volume_disp_active_ul, u))
        self.total_dispensed_ul_var.set(format_volume_ul_for_display(self._volume_disp_total_ul, u))

    def _build(self, ports: list[str]) -> None:
        ttk.Label(self, text="Display name").grid(row=0, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.nickname_var, width=36).grid(
            row=0, column=1, columnspan=5, sticky="w", padx=(6, 0)
        )

        # Own row — was row=0 cols 2–5 and sat under the display-name span, so live status often looked "missing".
        ttk.Label(self, text="Live status").grid(row=1, column=0, sticky="nw", pady=(4, 0))
        ttk.Label(self, textvariable=self.live_readout_var, wraplength=420).grid(
            row=1, column=1, columnspan=5, sticky="ew", padx=(6, 0), pady=(4, 0)
        )

        ttk.Label(self, text="COM Port").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.com_var = tk.StringVar(value=(ports[0] if ports else ""))
        self.com_combo = ttk.Combobox(self, textvariable=self.com_var, values=ports, width=12)
        self.com_combo.grid(row=2, column=1, sticky="w", padx=(6, 10), pady=(6, 0))

        ttk.Label(self, text="Address").grid(row=2, column=2, sticky="w", pady=(6, 0))
        self.address_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.address_var, width=6).grid(row=2, column=3, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self, text="Baud Rate").grid(row=2, column=4, sticky="w", padx=(10, 0), pady=(6, 0))
        # NE-1000 pumps often ship at 19200; NESP-Lib Port defaults to 9600 — pick what matches your pump menu.
        self.baud_var = tk.StringVar(value="19200")
        ttk.Combobox(self, textvariable=self.baud_var, values=BAUD_RATES, state="readonly", width=8).grid(
            row=2, column=5, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self, text="Syringe").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.syringe_var = tk.StringVar(value="BD 10 mL (10 cc)")
        self.syringe_combo = ttk.Combobox(
            self,
            textvariable=self.syringe_var,
            values=list(SYRINGE_PRESETS_MM.keys()),
            state="readonly",
            width=18,
        )
        self.syringe_combo.grid(row=3, column=1, sticky="w", padx=(6, 10), pady=(6, 0))
        self.syringe_combo.bind("<<ComboboxSelected>>", lambda _: self._on_syringe_preset_change())

        ttk.Label(self, text="Custom Diameter (mm)").grid(row=3, column=2, sticky="w", pady=(6, 0))
        self.custom_diameter_var = tk.StringVar(value="14.50")
        ttk.Entry(self, textvariable=self.custom_diameter_var, width=10).grid(row=3, column=3, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self, text="Rate Units").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.rate_units_var = tk.StringVar(value="mL/min")
        ttk.Combobox(self, textvariable=self.rate_units_var, values=RATE_UNITS, state="readonly", width=12).grid(
            row=4, column=1, sticky="w", padx=(6, 10), pady=(6, 0)
        )

        ttk.Label(self, text="Pumping Rate").grid(row=4, column=2, sticky="w", pady=(6, 0))
        self.rate_var = tk.StringVar(value="0.1")
        ttk.Entry(self, textvariable=self.rate_var, width=10).grid(row=4, column=3, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(self, text="Dispense Mode").grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.dispense_mode_var = tk.StringVar(value="Continuous")
        self.dispense_mode_combo = ttk.Combobox(
            self, textvariable=self.dispense_mode_var, values=DISPENSE_MODES, state="readonly", width=12
        )
        self.dispense_mode_combo.grid(row=5, column=1, sticky="w", padx=(6, 10), pady=(6, 0))
        self.dispense_mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_dispense_volume_visibility())
        self.dispense_mode_var.trace_add("write", lambda *_a: self._sync_dispense_volume_visibility())

        self.volume_ul_var = tk.StringVar(value="0")
        self._vol_disp_unit_var = tk.StringVar(value="uL")
        self._volume_ul_label = ttk.Label(self, text="Volume to Dispense")
        self._vol_disp_frame = ttk.Frame(self)
        self._volume_ul_entry = ttk.Entry(self._vol_disp_frame, textvariable=self.volume_ul_var, width=10)
        self._volume_ul_entry.pack(side="left")
        self._vol_disp_unit_combo = ttk.Combobox(
            self._vol_disp_frame,
            textvariable=self._vol_disp_unit_var,
            values=["uL", "mL", "L"],
            state="readonly",
            width=4,
        )
        self._vol_disp_unit_combo.pack(side="left", padx=(4, 0))

        ttk.Label(self, text="Direction").grid(row=7, column=0, sticky="w", pady=(6, 0))
        self.direction_var = tk.StringVar(value="Infuse")
        ttk.Combobox(self, textvariable=self.direction_var, values=["Infuse", "Withdraw"], state="readonly", width=12).grid(
            row=7, column=1, sticky="w", padx=(6, 10), pady=(6, 0)
        )

        ttk.Label(self, text="Volume Dispensed").grid(row=7, column=2, sticky="w", pady=(6, 0))
        self.dispensed_ul_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.dispensed_ul_var, width=14, state="readonly").grid(
            row=7, column=3, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        ttk.Label(self, text="Time (sec)").grid(row=8, column=0, sticky="w", pady=(6, 0))
        self.time_sec_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.time_sec_var, width=10, state="readonly").grid(
            row=8, column=1, sticky="w", padx=(6, 10), pady=(6, 0)
        )

        ttk.Label(self, text="Total Volume Dispensed").grid(row=8, column=2, sticky="w", pady=(6, 0))
        self.total_dispensed_ul_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.total_dispensed_ul_var, width=14, state="readonly").grid(
            row=8, column=3, sticky="w", padx=(6, 0), pady=(6, 0)
        )

        self.volume_display_unit_var = tk.StringVar(value="uL")
        vol_unit_fr = ttk.Frame(self)
        vol_unit_fr.grid(row=7, column=4, rowspan=2, sticky="nw", padx=(8, 0), pady=(6, 0))
        ttk.Label(vol_unit_fr, text="Display as").pack(anchor="w")
        self._volume_unit_combo = ttk.Combobox(
            vol_unit_fr,
            textvariable=self.volume_display_unit_var,
            values=VOLUME_DISPLAY_UNITS,
            state="readonly",
            width=5,
        )
        self._volume_unit_combo.pack(anchor="w", pady=(4, 0))
        self.volume_display_unit_var.trace_add("write", lambda *_a: self._refresh_dispensed_volume_display())
        self._refresh_dispensed_volume_display()

        btn_row = ttk.Frame(self)
        btn_row.grid(row=10, column=0, columnspan=6, sticky="ew", pady=(10, 0))
        for idx in range(4):
            btn_row.columnconfigure(idx, weight=1)
        ttk.Button(btn_row, text="Manual Connect", command=self.connect).grid(row=0, column=0, padx=4, sticky="ew")
        ttk.Button(btn_row, text="Disconnect", command=self.disconnect).grid(row=0, column=1, padx=4, sticky="ew")
        ttk.Button(btn_row, text="Reinitialize", command=self.reinitialize).grid(row=0, column=2, padx=4, sticky="ew")
        ttk.Button(btn_row, text="Reset Volume", command=self.reset_volume_async).grid(row=0, column=3, padx=4, sticky="ew")

        action_grid = ttk.Frame(self)
        action_grid.grid(row=11, column=0, columnspan=6, sticky="ew", pady=(8, 4))
        action_grid.columnconfigure(0, weight=1, uniform="act")
        action_grid.columnconfigure(1, weight=1, uniform="act")

        auto_btn = tk.Button(
            action_grid, text="Pump Auto-Connect", command=self.auto_connect_pump,
            font=("Segoe UI", 10, "bold"), pady=8, cursor="hand2",
        )
        auto_btn.grid(row=0, column=0, sticky="nsew", padx=(4, 3), pady=(0, 3))
        add_hover_glow(auto_btn)
        apply_btn = tk.Button(
            action_grid, text="Apply pump settings",
            command=lambda: self.app.apply_with_mode(self),
            cursor="hand2", pady=8, **BTN_APPLY_ACCENT,
        )
        apply_btn.grid(row=0, column=1, sticky="nsew", padx=(3, 4), pady=(0, 3))
        add_hover_glow(apply_btn, BTN_APPLY_ACCENT["bg"])
        run_btn = tk.Button(
            action_grid, text="Run", command=lambda: self.app.run_with_mode(self),
            bg="#28A745", fg="white", activebackground="#218838", activeforeground="white",
            font=("Segoe UI", 10, "bold"), pady=8, cursor="hand2", relief="raised", bd=2,
        )
        run_btn.grid(row=1, column=0, sticky="nsew", padx=(4, 3), pady=(3, 3))
        add_hover_glow(run_btn, "#28A745")
        stop_btn = tk.Button(
            action_grid, text="Stop", command=lambda: self.app.stop_with_mode(self),
            bg="#DC3545", fg="white", activebackground="#C82333", activeforeground="white",
            font=("Segoe UI", 10, "bold"), pady=8, cursor="hand2", relief="raised", bd=2,
        )
        stop_btn.grid(row=1, column=1, sticky="nsew", padx=(3, 4), pady=(3, 3))
        add_hover_glow(stop_btn, "#DC3545")
        switch_btn = tk.Button(
            action_grid, text="\u2192 Switch valve to this line",
            command=lambda: self.app.switch_valve_to_pump(self.pump_index),
            bg="#3B82F6", fg="white", activebackground="#1E40AF", activeforeground="white",
            font=("Segoe UI", 10, "bold"), pady=8, cursor="hand2", relief="raised", bd=2,
        )
        switch_btn.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=(4, 4), pady=(3, 0))
        add_hover_glow(switch_btn, "#3B82F6")

        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(self, textvariable=self.status_var).grid(row=12, column=0, columnspan=6, sticky="w", pady=(8, 0))
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

    def _set_connected_banner(
        self,
        com: str,
        baud_rate: int,
        address: int,
        pump_state: str,
        model_number: int,
    ) -> None:
        self._last_poll_err = None
        self.status_var.set(
            f"Connected — {com} @ {baud_rate} baud, addr {address}, NE model {model_number}, pump status: {pump_state}"
        )

    def _ui_mark_disconnected(self) -> None:
        self.set_status("Disconnected")
        self._last_poll_err = None

    def _show_poll_transport_error(self, detail: str, com: str, addr_s: str) -> None:
        self.live_readout_var.set(f"Can't read pump: {detail}")
        self.status_var.set(
            f"Serial/link problem on {com} (addr {addr_s}). Check baud rate, pump address, USB adapter, wiring, power."
        )

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
        """Continuous: hide volume-to-dispense. Volume: show it to the right of Dispense Mode."""
        if self.dispense_mode_var.get() == "Volume":
            self._volume_ul_label.grid(row=5, column=2, sticky="w", pady=(6, 0))
            self._vol_disp_frame.grid(row=5, column=3, sticky="w", padx=(6, 0), pady=(6, 0))
        else:
            self._volume_ul_label.grid_remove()
            self._vol_disp_frame.grid_remove()

    def _get_baud_rate(self) -> int:
        return int(self.baud_var.get().strip())

    def _verify_pump_readable(
        self, pump: Pump, com_label: str, baud_for_msg: Optional[int], addr_for_msg: Optional[int]
    ) -> tuple[str, int]:
        """
        Fail fast after Port/Pump creation: serial can open without a pump answering; requiring a
        status read matches what the periodic poller relies on anyway.
        """
        try:
            state = pump.status.name
        except Exception as exc:
            br = baud_for_msg if baud_for_msg is not None else "?"
            ad = addr_for_msg if addr_for_msg is not None else "?"
            raise RuntimeError(
                f"Serial opened ({com_label} @ {br} baud, addr {ad}) but the pump did not answer. "
                f"Wrong baud rate, pump address (NE-1000 menu), unplugged UART, USB adapter, or not a syringe pump on this port?"
            ) from exc
        return state, pump.model_number

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
                pump = Pump(port, address=address, model_number=Pump.MODEL_NUMBER_IGNORE)
                st_name, model_n = self._verify_pump_readable(pump, com_name, baud_rate, address)
                self.connection.port = port
                self.connection.pump = pump
                self.after(
                    0,
                    lambda c=com_name,
                    b=baud_rate,
                    ad=address,
                    ps=st_name,
                    mn=model_n: self._set_connected_banner(c, b, ad, ps, mn),
                )
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
            pump = Pump(port, address=address, model_number=Pump.MODEL_NUMBER_IGNORE)
            st_name, model_n = self._verify_pump_readable(pump, com_name, baud_rate, address)
            self.connection.port = port
            self.connection.pump = pump
            self.after(
                0,
                lambda c=com_name,
                b=baud_rate,
                ad=address,
                ps=st_name,
                mn=model_n: self._set_connected_banner(c, b, ad, ps, mn),
            )
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
        self.after(0, self._ui_mark_disconnected)

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
                self._last_poll_err = None
                self._volume_disp_active_ul = 0.0
                self._volume_disp_total_ul = 0.0
                self._refresh_dispensed_volume_display()
                self.time_sec_var.set("0")
                self.live_readout_var.set("\u2014")

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
                            pump = Pump(port, address=addr, model_number=Pump.MODEL_NUMBER_IGNORE)
                            st_name, model_n = self._verify_pump_readable(pump, com_name, baud, addr)
                            self.connection.port = port
                            self.connection.pump = pump
                            self.after(0, lambda a=addr: self.address_var.set(str(a)))
                            self.after(0, lambda b=baud: self.baud_var.set(str(b)))
                            self.after(
                                0,
                                lambda c=com_name,
                                a=addr,
                                b=baud,
                                mn=model_n,
                                ps=st_name: self._set_connected_banner(c, b, a, ps, mn),
                            )
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
        volume_raw = float(self.volume_ul_var.get().strip())
        vol_unit = self._vol_disp_unit_var.get().strip() if hasattr(self, "_vol_disp_unit_var") else "uL"
        if vol_unit == "mL":
            volume_ul = volume_raw * 1000.0
        elif vol_unit == "L":
            volume_ul = volume_raw * 1_000_000.0
        else:
            volume_ul = volume_raw
        volume_ml = max(volume_ul, 0.0) / 1000.0
        direction = PumpingDirection.INFUSE if self.direction_var.get() == "Infuse" else PumpingDirection.WITHDRAW

        pump.syringe_diameter = diameter_mm
        pump.pumping_rate = rate_ml_per_min
        pump.pumping_direction = direction
        if self.dispense_mode_var.get() == "Volume":
            if volume_ml <= 0.0:
                raise ValueError("Volume mode requires a positive 'Volume to Dispense'.")
            pump.pumping_volume = volume_ml

    def run_sync(self) -> None:
        pump = self._require_pump()
        pump.run(wait_while_running=False)
        self.run_start_ts = time.time()

    def run_sync_for_recipe(self, abort: Optional[threading.Event] = None) -> bool:
        """
        Used by recipe runners only. In **Volume** mode, blocks until the pump finishes the
        programmed dispense (or until *abort*). In **Continuous** mode, starts the pump and returns
        immediately (add a ``stop_pump`` step or delay before other pumps if you need ordering).
        Returns False if *abort* stopped the run.
        """
        pump = self._require_pump()
        poll = Pump.PUMPING_POLL_DELAY
        if self.dispense_mode_var.get() == "Volume":
            pump.run(wait_while_running=False)
            self.run_start_ts = time.time()
            while pump.running:
                if abort is not None and abort.is_set():
                    pump.stop(wait_while_running=True)
                    self.run_start_ts = None
                    return False
                time.sleep(poll)
            self.run_start_ts = None
        else:
            pump.run(wait_while_running=False)
            self.run_start_ts = time.time()
        return True

    def stop_sync(self) -> None:
        pump = self._require_pump()
        pump.stop(wait_while_running=True)
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
            self._last_poll_err = None
            self._volume_disp_active_ul = active_ul
            self._volume_disp_total_ul = total_ul
            self._refresh_dispensed_volume_display()
            u = self._current_volume_display_unit()
            self.set_status(f"Status: {status_name}")
            self.live_readout_var.set(
                f"{status_name} \u00b7 Active: {format_volume_ul_for_display(active_ul, u)} {u} \u00b7 "
                f"Total: {format_volume_ul_for_display(total_ul, u)} {u}"
            )

        self.after(0, update_ui)

    def read_status_async(self) -> None:
        self._run_in_thread(self.read_status_sync)

    def reset_volume_async(self) -> None:
        def work() -> None:
            pump = self._require_pump()
            pump.volume_infused_clear()
            pump.volume_withdrawn_clear()

            def reset_ui() -> None:
                self._volume_disp_active_ul = 0.0
                self._volume_disp_total_ul = 0.0
                self._refresh_dispensed_volume_display()
                self.set_status("Volume counters reset")

            self.after(0, reset_ui)

        self._run_in_thread(work)

    def maybe_poll_live_status(self) -> None:
        if self.connection.pump is None or self._polling:
            return

        self._polling = True

        def work() -> None:
            try:
                self.read_status_sync()
            except Exception as exc:
                raw = str(exc).strip() or exc.__class__.__name__
                if len(raw) > 160:
                    raw = raw[:157] + "..."
                prior = getattr(self, "_last_poll_err", None)
                if raw == prior:
                    return
                self._last_poll_err = raw
                com = self.com_var.get().strip() or "(no COM)"
                addr_str = "(?)"
                p = self.connection.pump
                if p is not None:
                    try:
                        addr_str = str(p.address)
                    except Exception:
                        pass
                self.after(
                    0,
                    lambda m=raw, cm=com, ad_s=addr_str: self._show_poll_transport_error(m, cm, ad_s),
                )
            finally:
                self._polling = False

        threading.Thread(target=work, daemon=True).start()

    def update_time_display(self) -> None:
        if self.run_start_ts is None:
            self.time_sec_var.set("0")
            return
        elapsed = int(max(0.0, time.time() - self.run_start_ts))
        self.time_sec_var.set(str(elapsed))


_VACUUM_LINE_RE = re.compile(
    r"VACUUM_KPA:\s*([-+]?\d+(?:\.\d+)?)\s*,\s*INHG:\s*([-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Brushed-motor EMI can flip bits on the USB-serial line, occasionally
# garbling a single '0' / '1' command byte from the GUI -> Arduino. Sending
# the command as a short burst makes it robust: the Arduino handles each
# byte idempotently (setting motor ON / OFF), and EMI would have to corrupt
# every copy in the burst (~430 us at 115200 baud for 5 bytes) to defeat it.
_VACUUM_CMD_REPEATS = 5


class VacuumPanel(ttk.LabelFrame):
    """Vacuum control using an Arduino serial command (1/0)."""

    def __init__(self, master: tk.Widget, ports: list[str]) -> None:
        super().__init__(master, text="Vacuum Control", padding=10)
        self._serial_lock = threading.Lock()
        self.serial_conn: Optional[serial.Serial] = None
        self.connected_com: Optional[str] = None
        self.connected_since_ts: Optional[float] = None
        self.is_on = False
        self._vacuum_blink_after_id: Optional[Any] = None
        self._vacuum_blink_phase = False
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()
        self.latest_bar: Optional[float] = None
        self._build(ports)

    def _build(self, ports: list[str]) -> None:
        ttk.Label(self, text="COM Port").grid(row=0, column=0, sticky="w")
        self.com_var = tk.StringVar(value=(ports[0] if ports else ""))
        self.com_combo = ttk.Combobox(self, textvariable=self.com_var, values=ports, width=12)
        self.com_combo.grid(row=0, column=1, sticky="w", padx=(6, 0))

        ttk.Label(self, text="Baud").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.baud_var = tk.StringVar(value="9600")
        ttk.Combobox(
            self,
            textvariable=self.baud_var,
            values=VACUUM_BAUD_RATES,
            state="readonly",
            width=10,
        ).grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))

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
        self._toggle_hover_base: Optional[str] = None
        self.toggle_btn.bind("<Enter>", self._on_toggle_enter, add=True)
        self.toggle_btn.bind("<Leave>", self._on_toggle_leave, add=True)
        self._set_button_color()

        self.status_var = tk.StringVar(value="Vacuum OFF")
        ttk.Label(self, textvariable=self.status_var).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.reply_var = tk.StringVar(value="Arduino reply: (none)")
        ttk.Label(self, textvariable=self.reply_var).grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.bar_var = tk.StringVar(value="Vacuum: --- bar")
        ttk.Label(self, textvariable=self.bar_var, font=("Segoe UI", 12, "bold")).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

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

    def _stop_vacuum_blink(self) -> None:
        if self._vacuum_blink_after_id is not None:
            try:
                self.after_cancel(self._vacuum_blink_after_id)
            except tk.TclError:
                pass
            self._vacuum_blink_after_id = None

    def _vacuum_blink_tick(self) -> None:
        if not self.is_on:
            self._vacuum_blink_after_id = None
            return
        self._vacuum_blink_phase = not self._vacuum_blink_phase
        if self._vacuum_blink_phase:
            self.toggle_btn.configure(bg="#1D4ED8", activebackground="#1E40AF")
        else:
            self.toggle_btn.configure(bg="#3B82F6", activebackground="#2563EB")
        self._vacuum_blink_after_id = self.after(450, self._vacuum_blink_tick)

    def _set_button_color(self) -> None:
        self._stop_vacuum_blink()
        if self.is_on:
            self.toggle_btn.configure(fg="white", activeforeground="white")
            self._vacuum_blink_phase = False
            self.toggle_btn.configure(bg="#3B82F6", activebackground="#2563EB")
            self._vacuum_blink_after_id = self.after(450, self._vacuum_blink_tick)
        else:
            self.toggle_btn.configure(
                bg="#E67E22", fg="white",
                activebackground="#D35400", activeforeground="white",
            )

    def _on_toggle_enter(self, _e: Any) -> None:
        self._toggle_hover_base = _resolve_color_to_hex(self.toggle_btn, self.toggle_btn.cget("bg"))
        self.toggle_btn.configure(bg=_brighten_hex(self._toggle_hover_base, 40))

    def _on_toggle_leave(self, _e: Any) -> None:
        if self._toggle_hover_base is not None:
            self.toggle_btn.configure(bg=self._toggle_hover_base)
            self._toggle_hover_base = None

    def _set_reply(self, text: str) -> None:
        self.reply_var.set(f"Arduino reply: {text}")

    def _set_vacuum_readout(self, kpa: Optional[float]) -> None:
        bar = -kpa / 100.0 if kpa is not None else None
        self.latest_bar = bar
        self.bar_var.set(
            f"Vacuum: {bar:+.2f} bar" if bar is not None else "Vacuum: --- bar"
        )

    def _reader_loop(self, conn: serial.Serial) -> None:
        """Continuously read newline-terminated lines from ``conn`` until stopped.

        Telemetry parsing happens in this background thread so the Tk main
        thread never sees raw lines. The freshest reading is cached on the
        panel (atomic attribute writes), and UI redraws are throttled to
        ~5 Hz. The Arduino currently streams at 2 Hz so the throttle is
        effectively a no-op at the moment, but it stays in place as a
        safety net in case the sketch is flashed with a higher rate.

        Non-telemetry lines are also filtered: only recognized status
        replies (MOTOR:ON / MOTOR:OFF) are forwarded to the UI, and even
        those are rate-limited. Any other line (e.g., a VACUUM_KPA line
        garbled mid-flight by motor EMI) is silently dropped. Without
        this, a noisy serial link can flood Tk's after() queue with
        bogus "Arduino reply" updates and freeze the GUI.
        """
        last_ui_push_ms = 0
        last_reply_push_ms = 0
        ui_min_interval_ms = 200  # cap UI updates at ~5 Hz
        max_line_len = 128
        while not self._reader_stop.is_set():
            try:
                if not conn.is_open:
                    break
                raw = conn.readline()
            except (serial.SerialException, OSError):
                self.after(0, lambda: self._set_reply("(disconnected)"))
                self.after(0, lambda: self._set_vacuum_readout(None))
                break
            except Exception as exc:
                msg = repr(exc)[:80]
                self.after(0, lambda m=msg: self._set_reply(f"(reader stopped: {m})"))
                break
            if self._reader_stop.is_set():
                break
            if not raw:
                continue
            try:
                line = raw.decode("utf-8", errors="ignore").strip()
            except Exception:
                continue
            if not line or len(line) > max_line_len:
                continue
            match = _VACUUM_LINE_RE.search(line)
            if match:
                try:
                    kpa = float(match.group(1))
                except ValueError:
                    continue
                self.latest_bar = -kpa / 100.0
                now_ms = int(time.monotonic() * 1000)
                if now_ms - last_ui_push_ms >= ui_min_interval_ms:
                    last_ui_push_ms = now_ms
                    self.after(0, lambda k=kpa: self._set_vacuum_readout(k))
            elif line.startswith("MOTOR:"):
                now_ms = int(time.monotonic() * 1000)
                if now_ms - last_reply_push_ms >= ui_min_interval_ms:
                    last_reply_push_ms = now_ms
                    self.after(0, lambda l=line: self._set_reply(l))

    def _start_reader_thread(self, conn: serial.Serial) -> None:
        existing = self._reader_thread
        if existing is not None and existing.is_alive():
            return
        self._reader_stop.clear()
        thread = threading.Thread(
            target=self._reader_loop, args=(conn,), daemon=True, name="VacuumSerialReader"
        )
        self._reader_thread = thread
        thread.start()

    def _stop_reader_thread(self) -> None:
        self._reader_stop.set()
        thread = self._reader_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._reader_thread = None

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

        try:
            baud = int(self.baud_var.get().strip())
        except (TypeError, ValueError):
            baud = 9600

        self._close_serial()
        self.serial_conn = serial.Serial(
            port=com_name, baudrate=baud, timeout=1, write_timeout=2
        )
        self.connected_com = com_name
        self.connected_since_ts = time.time()
        self._set_conn_status(f"Arduino: Connected on {com_name} @ {baud}")
        self.after(0, lambda: self._set_vacuum_readout(0.0))
        self._start_reader_thread(self.serial_conn)
        return self.serial_conn

    def _close_serial(self) -> None:
        self._stop_reader_thread()
        if self.serial_conn is not None:
            try:
                self.serial_conn.close()
            except Exception:
                pass
        self.serial_conn = None
        self.connected_com = None
        self.connected_since_ts = None
        self._set_conn_status("Arduino: Disconnected")
        self.after(0, lambda: self._set_vacuum_readout(None))

    def open_serial_explicit(self, com: str, baud: int = 9600) -> None:
        """Open vacuum serial on ``com`` (for recipe runner; does not require com_var yet)."""
        with self._serial_lock:
            self._close_serial()
            self.serial_conn = serial.Serial(
                port=com, baudrate=baud, timeout=1, write_timeout=2
            )
            self.connected_com = com
            self.connected_since_ts = time.time()
            self._start_reader_thread(self.serial_conn)
        self.after(0, lambda c=com: self.com_var.set(c))
        self.after(0, lambda b=baud: self.baud_var.set(str(b)))
        self.after(0, lambda c=com, b=baud: self._set_conn_status(f"Arduino: Connected on {c} @ {b}"))
        self.after(0, lambda: self._set_vacuum_readout(0.0))

    def close_serial_sync(self) -> None:
        with self._serial_lock:
            self._close_serial()
            self.is_on = False
        self.after(0, lambda: self.toggle_btn.config(text="Tap ON"))
        self.after(0, self._set_button_color)
        self.after(0, lambda: self._set_status("Vacuum OFF"))
        self.after(0, lambda: self._set_reply("(none)"))

    def send_vacuum_sync(self, on: bool) -> None:
        with self._serial_lock:
            conn = self._ensure_connected()
            self._wait_until_ready()
            value = ("1" if on else "0") * _VACUUM_CMD_REPEATS
            conn.write(value.encode("ascii"))
            conn.flush()
            self.is_on = on
        self.after(0, lambda o=on: self.toggle_btn.config(text="Tap OFF" if o else "Tap ON"))
        self.after(0, self._set_button_color)
        self.after(0, lambda o=on: self._set_status("Vacuum ON (recipe)" if o else "Vacuum OFF (recipe)"))

    def _send_value(self, value: str) -> None:
        with self._serial_lock:
            conn = self._ensure_connected()
            self._wait_until_ready()
            conn.write((value * _VACUUM_CMD_REPEATS).encode("ascii"))
            conn.flush()

    def connect_arduino(self) -> None:
        def work() -> None:
            with self._serial_lock:
                self._ensure_connected()
                self._wait_until_ready()
            self.after(0, lambda: self._set_status("Arduino connected and ready"))
            self.after(0, lambda: self._set_reply("ready"))

        threading.Thread(target=work, daemon=True).start()

    def disconnect_arduino(self) -> None:
        def work() -> None:
            with self._serial_lock:
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
                with self._serial_lock:
                    conn = self.serial_conn
                    if conn is not None and getattr(conn, "is_open", False):
                        try:
                            conn.write(b"0")
                            conn.flush()
                        except Exception:
                            pass
                    self.is_on = False
                self.after(0, lambda: self.toggle_btn.config(text="Tap ON"))
                self.after(0, self._set_button_color)
                self.after(0, lambda: self._set_status("Vacuum OFF (forced)"))
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda m=msg: self._set_status(f"Error forcing OFF: {m}"))

        threading.Thread(target=work, daemon=True).start()

    def close(self) -> None:
        self._stop_vacuum_blink()
        self._close_serial()


class ValvePanel(ttk.LabelFrame):
    """RUNZE SV-07 selector valve control: connect, switch ports, query status."""

    def __init__(self, master: tk.Widget, app: "PumpControllerApp", ports: list[str]) -> None:
        super().__init__(master, text="Selector Valve (RUNZE SV-07)", padding=10)
        self.app = app
        self._serial_lock = threading.Lock()
        self.driver: Optional[SV07] = None
        self.connected_com: Optional[str] = None
        self._busy = False
        self._port_buttons: list[tk.Button] = []
        # Live "Pump → Valve port" mapping table widgets (one row per pump).
        self._mapping_rows: list[dict[str, Any]] = []
        self._mapping_inner: Optional[ttk.Frame] = None
        self._mapping_status_var = tk.StringVar(value="")
        # Set true while we're rebuilding the table programmatically so the
        # per-row trace handlers don't echo back into a "save" cycle.
        self._mapping_suspend_trace = False
        # "Custom port labels" table (vents/bleeds/waste/etc.) — one row per
        # valve port up to ``max_ports``.
        self._label_rows: list[dict[str, Any]] = []
        self._label_inner: Optional[ttk.Frame] = None
        self._label_status_var = tk.StringVar(value="")
        self._label_suspend_trace = False
        self._build(ports)

    def _build(self, ports: list[str]) -> None:
        row = 0
        ttk.Label(self, text="COM Port").grid(row=row, column=0, sticky="w")
        self.com_var = tk.StringVar(value=(ports[0] if ports else ""))
        self.com_combo = ttk.Combobox(self, textvariable=self.com_var, values=ports, width=12)
        self.com_combo.grid(row=row, column=1, sticky="w", padx=(6, 12))

        ttk.Label(self, text="Baud").grid(row=row, column=2, sticky="w")
        self.baud_var = tk.StringVar(value="9600")
        ttk.Combobox(
            self, textvariable=self.baud_var, values=VALVE_BAUD_RATES, state="readonly", width=10,
        ).grid(row=row, column=3, sticky="w", padx=(6, 12))

        ttk.Label(self, text="Address").grid(row=row, column=4, sticky="w")
        self.addr_var = tk.StringVar(value="0")
        ttk.Entry(self, textvariable=self.addr_var, width=6).grid(row=row, column=5, sticky="w", padx=(6, 0))

        row += 1
        ttk.Label(self, text="Max ports").grid(row=row, column=0, sticky="w", pady=(6, 0))
        self.max_ports_var = tk.StringVar(value="6")
        max_combo = ttk.Combobox(
            self,
            textvariable=self.max_ports_var,
            values=[str(n) for n in VALVE_PORT_COUNT_OPTIONS],
            width=6,
            state="readonly",
        )
        max_combo.grid(row=row, column=1, sticky="w", padx=(6, 12), pady=(6, 0))
        max_combo.bind("<<ComboboxSelected>>", lambda _e: self._rebuild_port_grid())

        ttk.Label(self, text="Move timeout (s)").grid(row=row, column=2, sticky="w", pady=(6, 0))
        self.timeout_var = tk.StringVar(value="10")
        ttk.Entry(self, textvariable=self.timeout_var, width=6).grid(
            row=row, column=3, sticky="w", padx=(6, 12), pady=(6, 0)
        )

        row += 1
        conn_row = ttk.Frame(self)
        conn_row.grid(row=row, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        for c in range(4):
            conn_row.columnconfigure(c, weight=1)
        ttk.Button(conn_row, text="Connect Valve", command=self.connect_async).grid(row=0, column=0, padx=(0, 4), sticky="ew")
        ttk.Button(conn_row, text="Disconnect", command=self.disconnect_async).grid(row=0, column=1, padx=(4, 4), sticky="ew")
        ttk.Button(conn_row, text="Refresh status", command=self.refresh_status_async).grid(row=0, column=2, padx=(4, 4), sticky="ew")
        ttk.Button(conn_row, text="Reset to home", command=self.reset_home_async).grid(row=0, column=3, padx=(4, 0), sticky="ew")

        row += 1
        self.conn_status_var = tk.StringVar(value="Valve: Disconnected")
        ttk.Label(self, textvariable=self.conn_status_var).grid(
            row=row, column=0, columnspan=6, sticky="w", pady=(8, 0)
        )

        row += 1
        self.position_var = tk.StringVar(value="Current port: —")
        ttk.Label(self, textvariable=self.position_var, font=("Segoe UI", 12, "bold")).grid(
            row=row, column=0, columnspan=6, sticky="w", pady=(6, 0)
        )

        row += 1
        self.motion_var = tk.StringVar(value="Status: —")
        ttk.Label(self, textvariable=self.motion_var).grid(
            row=row, column=0, columnspan=6, sticky="w", pady=(2, 0)
        )

        row += 1
        ttk.Label(self, text="Move to port:").grid(row=row, column=0, columnspan=6, sticky="w", pady=(10, 4))

        row += 1
        self._port_grid = ttk.Frame(self)
        self._port_grid.grid(row=row, column=0, columnspan=6, sticky="ew")

        row += 1
        manual = ttk.Frame(self)
        manual.grid(row=row, column=0, columnspan=6, sticky="w", pady=(6, 0))
        ttk.Label(manual, text="Manual port").pack(side="left")
        self.manual_port_var = tk.StringVar(value="1")
        ttk.Entry(manual, textvariable=self.manual_port_var, width=4).pack(side="left", padx=(6, 6))
        ttk.Button(manual, text="Move + wait", command=self.move_manual_async).pack(side="left")

        # ----- Side-by-side: pump mapping + custom port labels ---------------
        row += 1
        tables_row = ttk.Frame(self)
        tables_row.grid(row=row, column=0, columnspan=6, sticky="ew", pady=(12, 0))
        tables_row.columnconfigure(0, weight=1, uniform="vtables")
        tables_row.columnconfigure(1, weight=1, uniform="vtables")

        # ----- Pump → Valve port mapping table -------------------------------
        map_frame = ttk.LabelFrame(
            tables_row,
            text="Pump → Valve port  (which line is each pump plumbed to)",
            padding=8,
        )
        map_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        for c in range(6):
            map_frame.columnconfigure(c, weight=1)

        ttk.Label(
            map_frame,
            text=("Set each pump's valve port here. The 'Switch valve to this line' "
                  "buttons on the Pumps tab use this mapping. Saved to pump_labels.json."),
            wraplength=380,
            justify="left",
        ).grid(row=0, column=0, columnspan=6, sticky="w")

        header = ttk.Frame(map_frame)
        header.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(8, 2))
        ttk.Label(header, text="Pump", width=6, anchor="w").pack(side="left", padx=(2, 6))
        ttk.Label(header, text="Nickname", anchor="w").pack(side="left", padx=(0, 6), expand=True, fill="x")
        ttk.Label(header, text="Valve port", width=12, anchor="w").pack(side="left")
        ttk.Label(header, text="", width=10).pack(side="left")

        self._mapping_inner = ttk.Frame(map_frame)
        self._mapping_inner.grid(row=2, column=0, columnspan=6, sticky="ew")

        bottom = ttk.Frame(map_frame)
        bottom.grid(row=3, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        ttk.Button(bottom, text="Clear all", command=self.clear_mapping_all).pack(side="left")
        ttk.Label(bottom, textvariable=self._mapping_status_var, foreground="#5A8F5A").pack(
            side="left", padx=(12, 0)
        )

        # ----- Custom port labels (vents / bleeds / waste / etc.) -----------
        label_frame = ttk.LabelFrame(
            tables_row,
            text="Custom port labels  (vents, bleeds, waste, atmosphere, …)",
            padding=8,
        )
        label_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        for c in range(6):
            label_frame.columnconfigure(c, weight=1)

        ttk.Label(
            label_frame,
            text=("Name any non-pump valve ports — bleed lines, vents, waste, "
                  "manual reservoirs. These show up on the port buttons above."),
            wraplength=380,
            justify="left",
        ).grid(row=0, column=0, columnspan=6, sticky="w")

        lbl_header = ttk.Frame(label_frame)
        lbl_header.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(8, 2))
        ttk.Label(lbl_header, text="Port", width=6, anchor="w").pack(side="left", padx=(2, 6))
        ttk.Label(lbl_header, text="Label", anchor="w").pack(side="left", padx=(0, 6), expand=True, fill="x")
        ttk.Label(lbl_header, text="", width=10).pack(side="left")

        self._label_inner = ttk.Frame(label_frame)
        self._label_inner.grid(row=2, column=0, columnspan=6, sticky="ew")

        lbl_bottom = ttk.Frame(label_frame)
        lbl_bottom.grid(row=3, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        ttk.Button(lbl_bottom, text="Clear all", command=self.clear_labels_all).pack(side="left")
        ttk.Label(lbl_bottom, textvariable=self._label_status_var, foreground="#5A8F5A").pack(
            side="left", padx=(12, 0)
        )

        self.columnconfigure(5, weight=1)
        self._rebuild_port_grid()
        self.refresh_mapping_table()
        self.refresh_label_table()

    def _rebuild_port_grid(self) -> None:
        for child in self._port_grid.winfo_children():
            child.destroy()
        self._port_buttons = []
        try:
            n = int(self.max_ports_var.get().strip())
        except ValueError:
            n = 6
        n = max(1, min(16, n))
        cols = 6
        for i in range(1, n + 1):
            btn = tk.Button(
                self._port_grid,
                text=f"Port {i}",
                command=lambda p=i: self.move_to_port_async(p),
                bg="#3A3A50",
                fg="white",
                font=("Segoe UI", 9, "bold"),
                relief="flat",
                bd=0,
                padx=8,
                pady=6,
                cursor="hand2",
                width=14,
                height=2,
                justify="center",
                wraplength=140,
            )
            r, c = divmod(i - 1, cols)
            btn.grid(row=r, column=c, padx=4, pady=4, sticky="ew")
            add_hover_glow(btn, "#3A3A50")
            self._port_buttons.append(btn)
        for c in range(cols):
            self._port_grid.columnconfigure(c, weight=1)
        self.refresh_port_button_text()
        # Custom labels table tracks max_ports too — keep them in sync.
        if self._label_inner is not None:
            self.refresh_label_table()

    def refresh_port_button_text(self) -> None:
        """Update each Move-to-port button's caption to reflect what's on the line."""
        for i, btn in enumerate(self._port_buttons, start=1):
            try:
                assignment = self.app.port_assignment_text(i)
            except Exception:
                assignment = ""
            text = f"Port {i}"
            if assignment:
                # Truncate long names so the button stays roughly the same size.
                short = assignment if len(assignment) <= 16 else assignment[:14] + "…"
                text = f"Port {i}\n{short}"
            try:
                if btn.cget("text") != text:
                    btn.configure(text=text)
            except tk.TclError:
                pass

    # ----- Pump → Valve port mapping table -------------------------------
    def refresh_mapping_table(self) -> None:
        """Update mapping rows to match ``app.num_pumps`` and the current map.

        If the row count already matches we update existing widgets in place
        so the user's spinbox doesn't get destroyed mid-edit.
        """
        if self._mapping_inner is None:
            return

        try:
            num_pumps = max(1, int(self.app.num_pumps))
        except Exception:
            num_pumps = 1
        live_map = dict(self.app._active_pump_port_map)

        if len(self._mapping_rows) == num_pumps:
            # In-place update: just sync the port and nickname StringVars.
            self._mapping_suspend_trace = True
            try:
                for row in self._mapping_rows:
                    i = row["pump_index"]
                    cur = live_map.get(i)
                    desired = "" if cur is None else str(cur)
                    if row["port_var"].get() != desired:
                        row["port_var"].set(desired)
            finally:
                self._mapping_suspend_trace = False
            self.refresh_mapping_nicknames()
            return

        # Row count changed → full rebuild.
        for child in self._mapping_inner.winfo_children():
            child.destroy()
        self._mapping_rows = []

        self._mapping_suspend_trace = True
        try:
            for i in range(1, num_pumps + 1):
                row_frame = ttk.Frame(self._mapping_inner)
                row_frame.pack(fill="x", pady=1)

                ttk.Label(row_frame, text=str(i), width=6, anchor="w").pack(side="left", padx=(2, 6))

                nickname = ""
                if i - 1 < len(self.app.panels):
                    try:
                        nickname = self.app.panels[i - 1].nickname_var.get().strip()
                    except Exception:
                        nickname = ""
                nickname_var = tk.StringVar(value=nickname)
                ttk.Label(row_frame, textvariable=nickname_var, anchor="w").pack(
                    side="left", padx=(0, 6), expand=True, fill="x"
                )

                cur = live_map.get(i)
                port_var = tk.StringVar(value=("" if cur is None else str(cur)))
                spin = ttk.Spinbox(
                    row_frame,
                    from_=0,
                    to=16,
                    textvariable=port_var,
                    width=6,
                )
                spin.pack(side="left", padx=(0, 6))

                clear_btn = ttk.Button(
                    row_frame,
                    text="Clear",
                    width=8,
                    command=lambda p=i: self._clear_mapping_row(p),
                )
                clear_btn.pack(side="left")

                row_state = {
                    "pump_index": i,
                    "nickname_var": nickname_var,
                    "port_var": port_var,
                    "spin": spin,
                }
                self._mapping_rows.append(row_state)
                # Trace must be added *after* row_state is appended so the
                # handler can find it. Capture ``i`` via a default arg to keep
                # closures correct across the loop.
                port_var.trace_add("write", lambda *_a, p=i: self._on_mapping_row_changed(p))
        finally:
            self._mapping_suspend_trace = False

    def refresh_mapping_nicknames(self) -> None:
        """Update only the nickname column (cheap, called from the live tick)."""
        for row in self._mapping_rows:
            i = row["pump_index"]
            if i - 1 < len(self.app.panels):
                try:
                    nick = self.app.panels[i - 1].nickname_var.get().strip()
                except Exception:
                    nick = ""
                if row["nickname_var"].get() != nick:
                    row["nickname_var"].set(nick)

    def _on_mapping_row_changed(self, pump_index: int) -> None:
        if self._mapping_suspend_trace:
            return
        row = next((r for r in self._mapping_rows if r["pump_index"] == pump_index), None)
        if row is None:
            return
        text = row["port_var"].get().strip()
        new_map = dict(self.app._active_pump_port_map)
        if text == "" or text == "0":
            new_map.pop(pump_index, None)
        else:
            try:
                port = int(text)
            except ValueError:
                return
            if port < 1 or port > 16:
                return
            new_map[pump_index] = port
        self.app.set_pump_port_mapping(new_map, persist=True)
        self._flash_mapping_status("Saved")

    def _clear_mapping_row(self, pump_index: int) -> None:
        new_map = dict(self.app._active_pump_port_map)
        if pump_index in new_map:
            new_map.pop(pump_index, None)
            self.app.set_pump_port_mapping(new_map, persist=True)
            self._flash_mapping_status("Cleared")

    def clear_mapping_all(self) -> None:
        if not self.app._active_pump_port_map:
            return
        self.app.set_pump_port_mapping({}, persist=True)
        self._flash_mapping_status("Cleared all")

    def _flash_mapping_status(self, text: str) -> None:
        self._mapping_status_var.set(text)
        self.after(1500, lambda: self._mapping_status_var.set(""))

    # ----- Custom port labels table --------------------------------------
    def refresh_label_table(self) -> None:
        """Sync the custom-label rows with the current ``max_ports`` value.

        In-place update when the row count is unchanged (preserves entry focus
        mid-edit); full rebuild only when ``max_ports`` actually changes.
        """
        if self._label_inner is None:
            return

        n = self._read_max_ports()
        live_labels = dict(self.app._active_port_labels)

        if len(self._label_rows) == n:
            self._label_suspend_trace = True
            try:
                for row in self._label_rows:
                    p = row["port"]
                    desired = live_labels.get(p, "")
                    if row["label_var"].get() != desired:
                        row["label_var"].set(desired)
                    self._update_label_row_hint(row)
            finally:
                self._label_suspend_trace = False
            return

        for child in self._label_inner.winfo_children():
            child.destroy()
        self._label_rows = []

        self._label_suspend_trace = True
        try:
            for p in range(1, n + 1):
                row_frame = ttk.Frame(self._label_inner)
                row_frame.pack(fill="x", pady=1)

                ttk.Label(row_frame, text=str(p), width=6, anchor="w").pack(side="left", padx=(2, 6))

                label_var = tk.StringVar(value=live_labels.get(p, ""))
                entry = ttk.Entry(row_frame, textvariable=label_var)
                entry.pack(side="left", padx=(0, 6), expand=True, fill="x")

                hint_var = tk.StringVar(value="")
                hint_label = ttk.Label(row_frame, textvariable=hint_var, foreground="#888888", width=14)
                hint_label.pack(side="left", padx=(0, 6))

                clear_btn = ttk.Button(
                    row_frame,
                    text="Clear",
                    width=8,
                    command=lambda port=p: self._clear_label_row(port),
                )
                clear_btn.pack(side="left")

                row_state: dict[str, Any] = {
                    "port": p,
                    "label_var": label_var,
                    "entry": entry,
                    "hint_var": hint_var,
                }
                self._label_rows.append(row_state)
                self._update_label_row_hint(row_state)
                label_var.trace_add("write", lambda *_a, port=p: self._on_label_row_changed(port))
        finally:
            self._label_suspend_trace = False

    def _update_label_row_hint(self, row: dict[str, Any]) -> None:
        """Show a small '(Pump N)' hint next to ports that are pump-mapped."""
        p = row["port"]
        pump_index: Optional[int] = None
        for pi, mapped in self.app._active_pump_port_map.items():
            if mapped == p:
                pump_index = pi
                break
        if pump_index is None:
            row["hint_var"].set("")
        else:
            row["hint_var"].set(f"(Pump {pump_index})")

    def _on_label_row_changed(self, port: int) -> None:
        if self._label_suspend_trace:
            return
        row = next((r for r in self._label_rows if r["port"] == port), None)
        if row is None:
            return
        text = row["label_var"].get().strip()
        new_labels = dict(self.app._active_port_labels)
        if text:
            new_labels[port] = text
        else:
            new_labels.pop(port, None)
        self.app.set_port_labels(new_labels, persist=True)
        self._flash_label_status("Saved")

    def _clear_label_row(self, port: int) -> None:
        if port not in self.app._active_port_labels:
            return
        new_labels = dict(self.app._active_port_labels)
        new_labels.pop(port, None)
        self.app.set_port_labels(new_labels, persist=True)
        self._flash_label_status("Cleared")

    def clear_labels_all(self) -> None:
        if not self.app._active_port_labels:
            return
        self.app.set_port_labels({}, persist=True)
        self._flash_label_status("Cleared all")

    def _flash_label_status(self, text: str) -> None:
        self._label_status_var.set(text)
        self.after(1500, lambda: self._label_status_var.set(""))

    def update_port_choices(self, ports: list[str]) -> None:
        current = self.com_var.get().strip()
        self.com_combo["values"] = ports
        if not current and ports:
            self.com_var.set(ports[0])

    def _set_conn_status(self, text: str) -> None:
        self.conn_status_var.set(text)

    def _set_motion(self, text: str) -> None:
        self.motion_var.set(f"Status: {text}")

    def _set_position(self, pos: Optional[int]) -> None:
        if pos is None or pos < 0:
            self.position_var.set("Current port: —")
        else:
            self.position_var.set(f"Current port: {pos}")
        self._refresh_port_button_highlight(pos)

    def _refresh_port_button_highlight(self, pos: Optional[int]) -> None:
        for i, btn in enumerate(self._port_buttons, start=1):
            try:
                if pos == i:
                    btn.configure(bg="#28A745")
                else:
                    btn.configure(bg="#3A3A50")
            except tk.TclError:
                pass

    def _read_max_ports(self) -> int:
        try:
            n = int(self.max_ports_var.get().strip())
        except ValueError:
            n = 6
        return max(1, min(16, n))

    def _read_timeout(self) -> float:
        try:
            t = float(self.timeout_var.get().strip())
        except ValueError:
            t = 10.0
        return max(1.0, t)

    @property
    def is_connected(self) -> bool:
        d = self.driver
        return d is not None and d.is_open

    def _ensure_open_locked(self) -> SV07:
        """Open serial inside the lock; raises if no COM port is set."""
        if self.driver is not None and self.driver.is_open:
            return self.driver
        com = self.com_var.get().strip()
        if not com:
            raise SV07Error("Select or type a COM port for the valve.")
        try:
            baud = int(self.baud_var.get().strip())
        except ValueError:
            baud = 9600
        try:
            addr = int(self.addr_var.get().strip())
        except ValueError:
            addr = 0
        d = SV07(port=com, baudrate=baud, address=addr, max_ports=self._read_max_ports())
        d.open()
        self.driver = d
        self.connected_com = com
        return d

    def _close_serial(self) -> None:
        d = self.driver
        if d is not None:
            try:
                d.close()
            except Exception:
                pass
        self.driver = None
        self.connected_com = None

    def connect_async(self) -> None:
        def work() -> None:
            try:
                with self._serial_lock:
                    d = self._ensure_open_locked()
                    d.max_ports = self._read_max_ports()
                pos = self._safe_query_position_with_lock()
                self.after(0, lambda c=d.port, b=d.baudrate, a=d.address: self._set_conn_status(
                    f"Valve: Connected on {c} @ {b} (addr {a})"
                ))
                self.after(0, lambda p=pos: self._set_position(p))
                self.after(0, lambda: self._set_motion("Idle"))
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda m=msg: self._set_conn_status(f"Valve: connect failed — {m}"))

        threading.Thread(target=work, daemon=True).start()

    def disconnect_async(self) -> None:
        def work() -> None:
            with self._serial_lock:
                self._close_serial()
            self.after(0, lambda: self._set_conn_status("Valve: Disconnected"))
            self.after(0, lambda: self._set_position(None))
            self.after(0, lambda: self._set_motion("—"))

        threading.Thread(target=work, daemon=True).start()

    def open_serial_explicit(
        self,
        com: str,
        baud: int = 9600,
        address: int = 0,
        max_ports: Optional[int] = None,
    ) -> None:
        """Open valve serial on ``com`` (used by the recipe runner)."""
        with self._serial_lock:
            self._close_serial()
            d = SV07(
                port=com,
                baudrate=baud,
                address=address,
                max_ports=max_ports if max_ports is not None else self._read_max_ports(),
            )
            d.open()
            self.driver = d
            self.connected_com = com
        self.after(0, lambda c=com: self.com_var.set(c))
        self.after(0, lambda b=baud: self.baud_var.set(str(b)))
        self.after(0, lambda a=address: self.addr_var.set(str(a)))
        if max_ports is not None:
            self.after(0, lambda mp=max_ports: self.max_ports_var.set(str(mp)))
            self.after(0, self._rebuild_port_grid)
        self.after(0, lambda c=com, b=baud, a=address: self._set_conn_status(
            f"Valve: Connected on {c} @ {b} (addr {a})"
        ))

    def close_serial_sync(self) -> None:
        with self._serial_lock:
            self._close_serial()
        self.after(0, lambda: self._set_conn_status("Valve: Disconnected"))
        self.after(0, lambda: self._set_position(None))
        self.after(0, lambda: self._set_motion("—"))

    def _safe_query_position_with_lock(self) -> Optional[int]:
        d = self.driver
        if d is None or not d.is_open:
            return None
        try:
            with self._serial_lock:
                return d.get_position()
        except Exception:
            return None

    def refresh_status_async(self) -> None:
        def work() -> None:
            try:
                with self._serial_lock:
                    d = self._ensure_open_locked()
                    text, busy = d.get_status()
                    pos = d.get_position()
                self.after(0, lambda t=text: self._set_motion(t))
                self.after(0, lambda p=pos: self._set_position(p))
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda m=msg: self._set_motion(f"Error: {m}"))

        threading.Thread(target=work, daemon=True).start()

    def reset_home_async(self) -> None:
        def work() -> None:
            try:
                with self._serial_lock:
                    d = self._ensure_open_locked()
                    d.reset_home()
                self.after(0, lambda: self._set_motion("Reset → home"))
                # Give the rotor a moment, then re-query.
                time.sleep(0.4)
                pos = self._safe_query_position_with_lock()
                self.after(0, lambda p=pos: self._set_position(p))
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda m=msg: self._set_motion(f"Error: {m}"))

        threading.Thread(target=work, daemon=True).start()

    def move_manual_async(self) -> None:
        try:
            p = int(self.manual_port_var.get().strip())
        except ValueError:
            self._set_motion("Error: manual port must be a number")
            return
        self.move_to_port_async(p)

    def move_to_port_async(self, port: int) -> None:
        def work() -> None:
            try:
                self.move_to_port_sync(port)
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda m=msg: self._set_motion(f"Error: {m}"))

        threading.Thread(target=work, daemon=True).start()

    def move_to_port_sync(self, port: int, abort: Optional[threading.Event] = None) -> int:
        """Block until the valve is at *port*. Returns the final reported position."""
        timeout = self._read_timeout()
        with self._serial_lock:
            d = self._ensure_open_locked()
            d.max_ports = self._read_max_ports()
            self.after(0, lambda p=port: self._set_motion(f"Moving → port {p}…"))
            final = d.move_and_wait(port, timeout_s=timeout, abort=abort)
        if final is None or final < 0:
            self.after(0, lambda: self._set_motion("Move aborted"))
            return -1
        self.after(0, lambda f=final: self._set_position(f))
        self.after(0, lambda f=final: self._set_motion(f"Idle (port {f})"))
        return final

    def close(self) -> None:
        with self._serial_lock:
            self._close_serial()


def pump_connection_snapshot(panel: "PumpPanel") -> dict[str, Any]:
    return {
        "com": panel.com_var.get().strip(),
        "baud": int(panel.baud_var.get().strip() or "19200"),
        "address": int(panel.address_var.get().strip() or "0"),
    }


class RecipeSequenceEditor(tk.Toplevel):
    """Second-level window to build ordered recipe steps (pumps, vacuum, selector valve, delays)."""

    def __init__(self, master: tk.Widget, app: "PumpControllerApp", recipe: dict[str, Any], on_saved: Callable[[], None]) -> None:
        super().__init__(master)
        self.app = app
        self._recipe_id = recipe.get("id")
        self._on_saved = on_saved
        self._steps: list[dict[str, Any]] = copy.deepcopy(recipe.get("steps") or [])

        theme = DARK if app.dark_mode else LIGHT
        self.title(f"Sequence — {recipe.get('name', 'Recipe')}")
        self.transient(master)
        self.geometry("880x660")
        self.minsize(720, 520)
        self.configure(bg=theme["bg"])

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text=(
                "Build steps with the buttons below first, then review and reorder them in the list "
                "(drag to reorder, Move up/down, Edit step…. Double-click edits). Steps run top→bottom "
                "when you use “Run sequence”. "
                'Selector valve: “Valve connect…”, then “Valve → port…” (any port number) or '
                "“Valve → custom inlet…” (names match the Selector Valve tab). Pump↔Port mapping is only "
                'for moving the valve when a pump’s line switches — vents/bleeds/other inlets don’t appear there.'
            ),
            wraplength=740,
        ).pack(anchor="w", pady=(0, 8))

        add_fr = ttk.LabelFrame(outer, text="Add step", padding=8)
        add_fr.pack(fill="x", pady=(0, 8))
        pumps_fr = ttk.LabelFrame(add_fr, text="Pumps", padding=4)
        pumps_fr.pack(fill="x")
        row1 = ttk.Frame(pumps_fr)
        row1.pack(fill="x")
        ttk.Button(row1, text="Delay…", command=self._add_delay).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(row1, text="Connect pump…", command=self._add_connect_pump).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(row1, text="Disconnect pump…", command=self._add_disconnect_pump).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(row1, text="Apply pump…", command=self._add_apply_pump).pack(side="left", padx=(0, 4), pady=2)
        row2 = ttk.Frame(pumps_fr)
        row2.pack(fill="x")
        ttk.Button(row2, text="Run pump…", command=self._add_run_pump).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(row2, text="Confirm line…", command=self._add_line_check).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(row2, text="Stop pump…", command=self._add_stop_pump).pack(side="left", padx=(0, 4), pady=2)

        vacuum_fr = ttk.LabelFrame(add_fr, text="Vacuum (Arduino)", padding=4)
        vacuum_fr.pack(fill="x", pady=(8, 0))
        vrow = ttk.Frame(vacuum_fr)
        vrow.pack(fill="x")
        ttk.Button(vrow, text="Vacuum connect…", command=self._add_vacuum_connect).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(vrow, text="Vacuum disconnect", command=self._add_vacuum_disconnect).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(vrow, text="Vacuum ON", command=lambda: self._push_step({"type": "vacuum_on"})).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(vrow, text="Vacuum OFF", command=lambda: self._push_step({"type": "vacuum_off"})).pack(side="left", padx=(0, 4), pady=2)

        valve_fr = ttk.LabelFrame(
            add_fr,
            text="Selector valve (SV-07) — connect once, then add port / line steps",
            padding=4,
        )
        valve_fr.pack(fill="x", pady=(8, 0))
        vrow1 = ttk.Frame(valve_fr)
        vrow1.pack(fill="x")
        ttk.Button(vrow1, text="Valve connect…", command=self._add_valve_connect).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(vrow1, text="Valve disconnect", command=lambda: self._push_step({"type": "valve_disconnect"})).pack(
            side="left", padx=(0, 4), pady=2
        )
        ttk.Button(vrow1, text="Pump↔Port mapping…", command=self._open_port_map_editor).pack(side="left", padx=(8, 4), pady=2)
        vrow2 = ttk.Frame(valve_fr)
        vrow2.pack(fill="x", pady=(4, 0))
        ttk.Button(vrow2, text="Valve → port…", command=self._add_valve_to_port).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(vrow2, text="Valve → line for pump…", command=self._add_valve_to_pump).pack(side="left", padx=(0, 4), pady=2)
        ttk.Button(vrow2, text="Valve → custom inlet…", command=self._add_valve_to_label).pack(side="left", padx=(0, 4), pady=2)

        tidy_fr = ttk.Frame(add_fr)
        tidy_fr.pack(fill="x", pady=(8, 0))
        ttk.Button(
            tidy_fr,
            text="Disconnect everything (stop pumps, vacuum OFF, close all COM)",
            command=self._add_disconnect_everything_step,
        ).pack(side="left", padx=(0, 4), pady=2)

        move_fr = ttk.Frame(outer)
        move_fr.pack(fill="x", pady=(0, 8))
        ttk.Button(move_fr, text="Move up", command=self._move_up).pack(side="left", padx=(0, 6))
        ttk.Button(move_fr, text="Move down", command=self._move_down).pack(side="left", padx=(0, 6))
        ttk.Button(move_fr, text="Edit step…", command=self._edit_selected_step).pack(side="left", padx=(0, 6))
        ttk.Button(move_fr, text="Remove step", command=self._remove).pack(side="left", padx=(0, 6))
        ttk.Button(move_fr, text="Step label…", command=self._label_selected_step).pack(side="left", padx=(16, 6))
        ttk.Button(move_fr, text="Clear step label", command=self._clear_step_label).pack(side="left", padx=(0, 6))

        ttk.Label(outer, text="Current sequence (runs top→bottom)", font=("Segoe UI", 9, "bold")).pack(
            anchor="w", pady=(4, 2)
        )
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

    def _pump_max(self) -> int:
        return _clamp_pump_count(getattr(self.app, "num_pumps", DEFAULT_NUM_PUMPS))

    def _pump_choices(self) -> list[str]:
        return [str(i) for i in range(1, self._pump_max() + 1)]

    def _pump_label(self) -> str:
        n = self._pump_max()
        return f"Pump (1–{n})"

    def _pump_label_short(self, action: str) -> str:
        n = self._pump_max()
        return f"Pump number (1–{n}) for {action}:"

    def _clamp_pump(self, raw: Any) -> int:
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return 1
        return max(1, min(self._pump_max(), v))

    def _ask_pump_int(self, title: str, prompt: str, initial: int = 1) -> Optional[int]:
        return simpledialog.askinteger(
            title,
            prompt,
            parent=self,
            minvalue=1,
            maxvalue=self._pump_max(),
            initialvalue=max(1, min(self._pump_max(), initial)),
        )

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
            cur_p = self._clamp_pump(st.get("pump", 1))
            p = self._ask_pump_int("Disconnect pump", self._pump_label_short("disconnect"), cur_p)
            if p is not None:
                self._commit_step(index, {"type": "disconnect_pump", "pump": int(p)})
        elif t == "apply_pump":
            self._open_apply_pump_dialog(edit_index=index, initial=st)
        elif t == "run_pump":
            cur_p = self._clamp_pump(st.get("pump", 1))
            p = self._ask_pump_int("Run pump", self._pump_label_short("run"), cur_p)
            if p is not None:
                self._commit_step(index, {"type": "run_pump", "pump": int(p)})
        elif t == "line_check":
            cur_p = self._clamp_pump(st.get("pump", 1))
            p = self._ask_pump_int("Confirm line", self._pump_label_short("line check"), cur_p)
            if p is not None:
                self._commit_step(index, {"type": "line_check", "pump": int(p)})
        elif t == "stop_pump":
            cur_p = self._clamp_pump(st.get("pump", 1))
            p = self._ask_pump_int("Stop pump", self._pump_label_short("stop"), cur_p)
            if p is not None:
                self._commit_step(index, {"type": "stop_pump", "pump": int(p)})
        elif t == "vacuum_connect":
            self._open_vacuum_connect_dialog(edit_index=index, initial=st)
        elif t == "valve_connect":
            self._open_valve_connect_dialog(edit_index=index, initial=st)
        elif t == "valve_to_port":
            self._open_valve_to_port_dialog(edit_index=index, initial=st)
        elif t == "valve_to_pump":
            cur_p = self._clamp_pump(st.get("pump", 1))
            p = self._ask_pump_int(
                "Valve → line for pump",
                f"Move valve to the port mapped to which pump (1–{self._pump_max()})?",
                cur_p,
            )
            if p is not None:
                self._commit_step(index, {"type": "valve_to_pump", "pump": int(p)})
        elif t == "valve_to_label":
            self._open_valve_to_label_dialog(edit_index=index, initial=st)
        elif t in ("vacuum_disconnect", "vacuum_on", "vacuum_off", "valve_disconnect", "disconnect_everything"):
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
        ttk.Label(fr, text=self._pump_label()).grid(row=0, column=0, sticky="w")
        ttk.Combobox(fr, textvariable=pv, values=self._pump_choices(), width=6, state="readonly").grid(row=0, column=1, sticky="w", padx=6)
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
            if p < 1 or p > self._pump_max():
                return
            self._commit_step(
                edit_index,
                {"type": "connect_pump", "pump": p, "com": cv.get().strip(), "baud": baud, "address": addr},
            )
            d.destroy()

        ttk.Button(fr, text="Cancel", command=d.destroy).grid(row=4, column=0, pady=(12, 0), sticky="w")
        ttk.Button(fr, text=("Save" if is_edit else "Add"), command=ok).grid(row=4, column=1, pady=(12, 0), sticky="e")

    def _add_disconnect_pump(self) -> None:
        p = self._ask_pump_int("Disconnect pump", self._pump_label_short("disconnect"))
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
        ttk.Label(fr, text=self._pump_label()).grid(row=0, column=0, sticky="w")
        pump_cb = ttk.Combobox(fr, textvariable=pv, values=self._pump_choices(), width=6, state="readonly")
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
                pv.set(str(max(1, min(self._pump_max(), int(initial.get("pump", 1))))))
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
            if p < 1 or p > self._pump_max():
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
        p = self._ask_pump_int("Run pump", self._pump_label_short("run"))
        if p is not None:
            self._push_step({"type": "run_pump", "pump": int(p)})

    def _add_line_check(self) -> None:
        p = self._ask_pump_int(
            "Confirm line",
            f"Pump number (1–{self._pump_max()}) — pause until operator confirms that line is open:",
        )
        if p is not None:
            self._push_step({"type": "line_check", "pump": int(p)})

    def _add_stop_pump(self) -> None:
        p = self._ask_pump_int("Stop pump", self._pump_label_short("stop"))
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

    def _add_disconnect_everything_step(self) -> None:
        self._push_step({"type": "disconnect_everything"})

    def _editor_valve_port_cap(self) -> int:
        vp = getattr(self.app, "valve_panel", None)
        if vp is None:
            return 16
        try:
            return max(1, min(16, int(str(vp.max_ports_var.get()).strip())))
        except (ValueError, TypeError):
            return 16

    # --- Valve step dialogs --------------------------------------------------

    def _add_valve_connect(self) -> None:
        self._open_valve_connect_dialog()

    def _open_valve_connect_dialog(
        self, edit_index: Optional[int] = None, initial: Optional[dict[str, Any]] = None
    ) -> None:
        is_edit = edit_index is not None
        d = tk.Toplevel(self)
        d.title("Edit valve connect" if is_edit else "Valve connect (RUNZE SV-07)")
        d.transient(self)
        d.geometry("420x220")
        fr = ttk.Frame(d, padding=12)
        fr.pack(fill="both", expand=True)

        com0 = "COM4"
        baud0 = "9600"
        addr0 = "0"
        max_ports0 = "6"
        if initial and initial.get("type") == "valve_connect":
            com0 = str(initial.get("com") or "COM4").strip() or "COM4"
            try:
                baud0 = str(int(initial.get("baud", 9600)))
            except (TypeError, ValueError):
                baud0 = "9600"
            try:
                addr0 = str(int(initial.get("address", 0)))
            except (TypeError, ValueError):
                addr0 = "0"
            try:
                max_ports0 = str(int(initial.get("max_ports", 6)))
            except (TypeError, ValueError):
                max_ports0 = "6"

        cv = tk.StringVar(value=com0)
        bv = tk.StringVar(value=baud0)
        av = tk.StringVar(value=addr0)
        mv = tk.StringVar(value=max_ports0)

        ttk.Label(fr, text="Valve COM port").grid(row=0, column=0, sticky="w")
        ttk.Combobox(fr, textvariable=cv, values=self._com_port_values(com0), width=14).grid(
            row=0, column=1, sticky="w", padx=8
        )
        ttk.Label(fr, text="Baud rate").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            fr, textvariable=bv, values=list(VALVE_BAUD_RATES), width=10, state="readonly"
        ).grid(row=1, column=1, sticky="w", padx=8, pady=(8, 0))
        ttk.Label(fr, text="Address").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(fr, textvariable=av, width=8).grid(row=2, column=1, sticky="w", padx=8, pady=(8, 0))
        ttk.Label(fr, text="Max ports").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            fr,
            textvariable=mv,
            values=[str(n) for n in VALVE_PORT_COUNT_OPTIONS],
            width=8,
            state="readonly",
        ).grid(row=3, column=1, sticky="w", padx=8, pady=(8, 0))

        def ok() -> None:
            com = cv.get().strip()
            if not com:
                messagebox.showerror("Valve connect", "Enter a COM port.", parent=d)
                return
            try:
                baud = int(bv.get().strip())
                addr = int(av.get().strip())
                max_ports = int(mv.get().strip())
            except ValueError:
                messagebox.showerror("Valve connect", "Invalid number.", parent=d)
                return
            self._commit_step(
                edit_index,
                {
                    "type": "valve_connect",
                    "com": com,
                    "baud": baud,
                    "address": addr,
                    "max_ports": max_ports,
                },
            )
            d.destroy()

        bf = ttk.Frame(fr)
        bf.grid(row=4, column=0, columnspan=2, pady=(14, 0), sticky="w")
        ttk.Button(bf, text="Cancel", command=d.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(bf, text=("Save" if is_edit else "Add step"), command=ok).pack(side="left")

    def _add_valve_to_port(self) -> None:
        self._open_valve_to_port_dialog()

    def _open_valve_to_port_dialog(
        self, edit_index: Optional[int] = None, initial: Optional[dict[str, Any]] = None
    ) -> None:
        cap = max(1, min(16, self._editor_valve_port_cap()))
        cur_port = 1
        if initial and initial.get("type") == "valve_to_port":
            try:
                cur_port = max(1, min(cap, int(initial.get("port", 1))))
            except (TypeError, ValueError):
                cur_port = 1

        labels = dict(getattr(self.app, "_active_port_labels", {}) or {})
        opt_lines: list[str] = []
        port_by_line: dict[str, int] = {}
        for n in range(1, cap + 1):
            lab = str(labels.get(n, "")).strip()
            disp = f"Port {n} — {lab}" if lab else f"Port {n}"
            opt_lines.append(disp)
            port_by_line[disp] = n

        is_edit = edit_index is not None
        d = tk.Toplevel(self)
        d.title("Edit valve → port" if is_edit else "Valve → port")
        d.transient(self)
        d.grab_set()
        fr = ttk.Frame(d, padding=12)
        fr.pack(fill="both", expand=True)

        hint = (
            f"Configured max ports from the Selector Valve tab: {cap}. "
            "Pick a labelled line or enter a port number."
        )
        ttk.Label(fr, text=hint, wraplength=400, justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(fr, text="Port / line").grid(row=1, column=0, sticky="w")
        combo_var = tk.StringVar(value=opt_lines[cur_port - 1] if 1 <= cur_port <= len(opt_lines) else f"Port {cur_port}")
        combo = ttk.Combobox(fr, textvariable=combo_var, values=opt_lines, width=42, state="readonly")
        combo.grid(row=1, column=1, sticky="ew", padx=(8, 0))

        ttk.Label(fr, text="Port number").grid(row=2, column=0, sticky="w", pady=(8, 0))
        port_var = tk.StringVar(value=str(cur_port))
        sp = tk.Spinbox(fr, textvariable=port_var, from_=1, to=cap, width=8, increment=1)
        sp.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        def sync_spin_from_combo(*_a: Any) -> None:
            raw = combo_var.get()
            p = port_by_line.get(raw)
            if p is not None:
                port_var.set(str(p))

        def sync_combo_from_spin(_evt: Optional[tk.Event] = None) -> None:
            try:
                p = max(1, min(cap, int(str(port_var.get()).strip())))
            except (TypeError, ValueError):
                return
            if 1 <= p <= len(opt_lines):
                combo_var.set(opt_lines[p - 1])

        combo.bind("<<ComboboxSelected>>", sync_spin_from_combo)
        sp.configure(command=lambda *args: sync_combo_from_spin())
        sp.bind("<FocusOut>", sync_combo_from_spin)
        sp.bind("<Return>", sync_combo_from_spin)

        def parse_port_from_ui() -> Optional[int]:
            try:
                n = int(str(port_var.get()).strip())
                if 1 <= n <= cap:
                    return n
            except (TypeError, ValueError):
                pass
            raw_key = combo_var.get()
            p = port_by_line.get(raw_key)
            return int(p) if p is not None else None

        def ok() -> None:
            pn = parse_port_from_ui()
            if pn is None:
                messagebox.showerror("Valve → port", f"Pick a valid port between 1 and {cap}.", parent=d)
                return
            self._commit_step(edit_index, {"type": "valve_to_port", "port": int(pn)})
            d.destroy()

        bf = ttk.Frame(fr)
        bf.grid(row=3, column=0, columnspan=2, pady=(14, 0), sticky="w")
        ttk.Button(bf, text="Cancel", command=d.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(bf, text=("Save" if is_edit else "Add step"), command=ok).pack(side="left")
        fr.columnconfigure(1, weight=1)

    def _add_valve_to_pump(self) -> None:
        p = self._ask_pump_int(
            "Valve → line for pump",
            f"Move valve to the port mapped to which pump (1–{self._pump_max()})?",
        )
        if p is not None:
            self._push_step({"type": "valve_to_pump", "pump": int(p)})

    def _add_valve_to_label(self) -> None:
        self._open_valve_to_label_dialog()

    def _open_valve_to_label_dialog(
        self, edit_index: Optional[int] = None, initial: Optional[dict[str, Any]] = None
    ) -> None:
        """Add or edit a ``valve_to_label`` step (custom inlets — vents, bleeds, etc.)."""
        is_edit = edit_index is not None
        # Pull the current set of custom labels from the live app state so the
        # combobox suggests the names the user has already configured. The
        # final stored value is whatever they confirm (typing a new name is OK).
        active_labels = dict(getattr(self.app, "_active_port_labels", {}) or {})
        suggestions = [
            f"{lab}  (port {port})"
            for port, lab in sorted(active_labels.items(), key=lambda kv: kv[0])
        ]

        d = tk.Toplevel(self)
        d.title("Valve → custom inlet")
        d.transient(self)
        d.grab_set()
        fr = ttk.Frame(d, padding=12)
        fr.pack(fill="both", expand=True)
        ttk.Label(
            fr,
            text=("Pick (or type) a custom port label. The label is resolved to "
                  "a port at run time using the labels on the Selector Valve tab."),
            wraplength=380,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(fr, text="Label").grid(row=1, column=0, sticky="w")
        initial_text = ""
        if initial and initial.get("type") == "valve_to_label":
            initial_text = str(initial.get("label", "")).strip()
        label_var = tk.StringVar(value=initial_text)
        combo = ttk.Combobox(fr, textvariable=label_var, values=suggestions, width=36)
        combo.grid(row=1, column=1, sticky="ew", padx=(8, 0))

        def ok() -> None:
            raw = label_var.get().strip()
            if not raw:
                messagebox.showerror("Valve → custom inlet", "Pick or type a label.", parent=d)
                return
            # If the user picked a suggestion like 'Vent  (port 5)', strip the
            # trailing port hint so we save just the label text.
            cleaned = raw
            if "  (port" in cleaned:
                cleaned = cleaned.split("  (port", 1)[0].strip()
            self._commit_step(edit_index, {"type": "valve_to_label", "label": cleaned})
            d.destroy()

        bf = ttk.Frame(fr)
        bf.grid(row=2, column=0, columnspan=2, pady=(14, 0), sticky="w")
        ttk.Button(bf, text="Cancel", command=d.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(bf, text=("Save" if is_edit else "Add step"), command=ok).pack(side="left")
        fr.columnconfigure(1, weight=1)

    def _open_port_map_editor(self) -> None:
        """Per-recipe pump↔valve-port mapping editor. Saves to the recipe immediately."""
        rid = self._recipe_id
        if not rid:
            messagebox.showerror(
                "Pump↔Port mapping",
                "Recipe has no id; save the recipe from the main Recipes window first.",
                parent=self,
            )
            return
        all_recipes = load_stored_recipes()
        target = next((r for r in all_recipes if r.get("id") == rid), None)
        if target is None:
            messagebox.showerror("Pump↔Port mapping", "Recipe not found in file.", parent=self)
            return

        existing_map = recipe_pump_port_map(target)
        n_pumps = self._pump_max()
        cap_ports = max(1, min(16, self._editor_valve_port_cap()))
        labels_live = dict(getattr(self.app, "_active_port_labels", {}) or {})
        ref_parts = []
        for pn in range(1, cap_ports + 1):
            lb_txt = str(labels_live.get(pn, "")).strip()
            ref_parts.append(
                f'  Port {pn}: "{lb_txt}"'
                if lb_txt
                else f"  Port {pn}: (no label on Selector Valve tab yet)"
            )
        ref_txt = "\n".join(ref_parts)

        theme = DARK if self.app.dark_mode else LIGHT
        d = tk.Toplevel(self)
        d.title(f"Pump↔Port mapping — {target.get('name', '')}")
        d.transient(self)
        hdr_h = 110 + 36 * max(1, n_pumps)
        d.geometry(f"480x{min(760, hdr_h + 22 * max(6, cap_ports))}")

        outer_map = ttk.Frame(d)
        outer_map.pack(fill="both", expand=True)
        cv = tk.Canvas(outer_map, highlightthickness=0)
        vsb = ttk.Scrollbar(outer_map, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=vsb.set)
        cv.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        fr = ttk.Frame(cv, padding=12)
        cw = cv.create_window((0, 0), window=fr, anchor="nw")

        def _canvas_configure(_evt: tk.Event) -> None:
            try:
                cv.itemconfigure(cw, width=cv.winfo_width())
            except tk.TclError:
                pass
            cv.configure(scrollregion=cv.bbox("all"))

        cv.bind("<Configure>", _canvas_configure)
        fr.bind("<Configure>", lambda _e: cv.configure(scrollregion=cv.bbox("all")))

        def _on_mousewheel(evt: tk.Event) -> None:
            if evt.delta:
                cv.yview_scroll(int(-1 * (evt.delta / 120)), "units")

        d.bind("<MouseWheel>", _on_mousewheel)

        row_i = 0
        ttk.Label(
            fr,
            text=(
                "Pump↔Port mapping assigns which valve position feeds each syringe pump when you use "
                "“Valve → line for pump N” in your sequence.\n\n"
                "You get one editable row per pump that exists in your layout—not every rotor port "
                "is edited here.\n\n"
                "Non-pump lines (vents, sealed caps, bleed/air, wastes, …) remain full valve ports: "
                'switch to them using sequence steps “Valve → port…” or “Valve → custom inlet…”. '
                "Custom inlet names live on main window → Selector Valve tab → Custom port labels."
            ),
            wraplength=430,
            justify="left",
        ).grid(row=row_i, column=0, columnspan=3, sticky="w", pady=(0, 6))
        row_i += 1

        lf = ttk.LabelFrame(fr, text=f"All {cap_ports} valve ports (reference)", padding=(8, 6))
        lf.grid(row=row_i, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ttk.Label(
            lf,
            text="Edit labels on main window → Selector Valve tab. Monospace layout for quick scanning:",
            wraplength=400,
            font=("Segoe UI", 8),
        ).pack(anchor="w")
        mono = tk.Text(
            lf,
            height=min(14, max(6, cap_ports + 2)),
            width=48,
            font=("Consolas", 9),
            wrap="none",
            relief="flat",
            bd=0,
            highlightthickness=0,
            state="disabled",
            bg=theme["input"],
            fg=theme["fg"],
        )
        mono.pack(anchor="w", pady=(4, 0))
        mono.configure(state="normal")
        mono.delete("1.0", tk.END)
        mono.insert(tk.END, ref_txt + "\n")
        mono.configure(state="disabled")

        row_i += 1
        hdr_row = row_i
        nicks = self.app.pump_nickname_map()
        ttk.Label(fr, text="Pump", font=("Segoe UI", 9, "bold")).grid(row=hdr_row, column=0, sticky="w", pady=(0, 2))
        ttk.Label(fr, text="Nickname", font=("Segoe UI", 9, "bold")).grid(row=hdr_row, column=1, sticky="w", pady=(0, 2))
        ttk.Label(fr, text=f"Valve port 1–{cap_ports}", font=("Segoe UI", 9, "bold")).grid(
            row=hdr_row, column=2, sticky="w", pady=(0, 2)
        )

        port_vars: dict[int, tk.StringVar] = {}
        for offset, i in enumerate(range(1, n_pumps + 1)):
            rr = hdr_row + 1 + offset
            initial_port = existing_map.get(i)
            v = tk.StringVar(value=str(initial_port) if initial_port else "")
            port_vars[i] = v
            ttk.Label(fr, text=f"Pump {i}").grid(row=rr, column=0, sticky="w", pady=2)
            ttk.Label(fr, text=nicks.get(i, "")).grid(row=rr, column=1, sticky="w", pady=2)
            ttk.Entry(fr, textvariable=v, width=8).grid(row=rr, column=2, sticky="w", pady=2)

        def save_map() -> None:
            new_map: dict[str, int] = {}
            for i, var in port_vars.items():
                raw = var.get().strip()
                if not raw:
                    continue
                try:
                    n = int(raw)
                except ValueError:
                    messagebox.showerror(
                        "Pump↔Port mapping",
                        f"Pump {i}: '{raw}' is not a valid port number.",
                        parent=d,
                    )
                    return
                if n < 1 or n > cap_ports:
                    messagebox.showerror(
                        "Pump↔Port mapping",
                        f"Pump {i}: port must be between 1 and {cap_ports} given the valve panel's max-port setting.",
                        parent=d,
                    )
                    return
                new_map[str(i)] = n
            target["pump_port_map"] = new_map
            save_stored_recipes(all_recipes)
            self._on_saved()
            messagebox.showinfo("Pump↔Port mapping", "Mapping saved to recipe.", parent=d)
            d.destroy()

        last_pump_row = hdr_row + n_pumps
        bf = ttk.Frame(fr)
        bf.grid(row=last_pump_row + 1, column=0, columnspan=3, pady=(14, 0), sticky="w")
        ttk.Button(bf, text="Cancel", command=d.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(bf, text="Save mapping", command=save_map).pack(side="left")

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
    """Save / load N-pump parameter sets; apply to panels or apply+run connected pumps."""

    def __init__(self, master: tk.Widget, app: "PumpControllerApp") -> None:
        super().__init__(master, padding=4)
        self.app = app
        self._recipes: list[dict[str, Any]] = []

        ttk.Label(self, text="Recipes", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(
            self,
            text=(
                "Saves pump settings + COM/baud/address for every pump panel currently shown, "
                "plus valve serial settings when the valve tab is populated. Full protocols use "
                "“Edit sequence…”: ordered connect steps (pumps, vacuum, valve), timed delays, "
                "vacuum ON/OFF, valve line changes (“Valve → port…” / “Valve → custom inlet…”), "
                "pump runs, then “Run sequence”. Use “Apply + Run all” only for simple recipes "
                "with no sequence."
            ),
            wraplength=440,
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
        n = len(self.app.panels)
        pumps_block: dict[str, Any] = {}
        conns_block: dict[str, Any] = {}
        labels_block: dict[str, str] = {}
        for i, panel in enumerate(self.app.panels, start=1):
            pumps_block[str(i)] = panel.snapshot_settings()
            conns_block[str(i)] = pump_connection_snapshot(panel)
            labels_block[str(i)] = panel.nickname_var.get().strip()

        recipe: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "name": name,
            "num_pumps": n,
            "pumps": pumps_block,
            "pump_conns": conns_block,
            "pump_labels": labels_block,
        }
        valve_snap = self.app.valve_connection_snapshot()
        if valve_snap is not None:
            recipe["valve_conn"] = valve_snap
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
        self.app._confirm_and_run_recipe(rec)

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
        self.app._confirm_and_run_recipe(rec)

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


class OverviewTab(ttk.Frame):
    """Compact landing page showing live status + critical actions for every device."""

    PUMP_CARD_COLS = 3

    def __init__(self, master: tk.Widget, app: "PumpControllerApp") -> None:
        super().__init__(master)
        self.app = app
        self._pump_cards: list[dict[str, Any]] = []
        self._vac_bar_var = tk.StringVar(value="Vacuum: --- bar")
        self._vac_status_var = tk.StringVar(value="Disconnected")
        self._vac_toggle_btn: Optional[tk.Button] = None
        self._valve_pos_var = tk.StringVar(value="Current: —")
        self._valve_status_var = tk.StringVar(value="Disconnected")
        self._valve_port_grid: Optional[ttk.Frame] = None
        self._valve_port_buttons: list[tk.Button] = []
        self._build()

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=2)
        self.rowconfigure(1, weight=1)

        # --- PUMPS section ---------------------------------------------------
        self._pumps_section = ttk.LabelFrame(self, text="Pumps", padding=8)
        self._pumps_section.grid(row=0, column=0, sticky="nsew", padx=4, pady=(0, 6))
        self._pumps_grid = ttk.Frame(self._pumps_section)
        self._pumps_grid.pack(fill="both", expand=True)
        self._build_pump_cards()

        # --- Bottom section: vacuum + valve side-by-side ---------------------
        bottom = ttk.Frame(self)
        bottom.grid(row=1, column=0, sticky="nsew", padx=4)
        bottom.columnconfigure(0, weight=1, uniform="b")
        bottom.columnconfigure(1, weight=1, uniform="b")
        bottom.rowconfigure(0, weight=1)

        # Vacuum card
        vac_card = ttk.LabelFrame(bottom, text="Vacuum / Pressure", padding=10)
        vac_card.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        ttk.Label(vac_card, textvariable=self._vac_bar_var, font=("Segoe UI", 18, "bold")).pack(anchor="w", pady=(0, 4))
        ttk.Label(vac_card, textvariable=self._vac_status_var, foreground="#888").pack(anchor="w", pady=(0, 8))
        btn_fr = ttk.Frame(vac_card)
        btn_fr.pack(fill="x")
        self._vac_toggle_btn = tk.Button(
            btn_fr, text="Vacuum ON", command=self._toggle_vacuum,
            bg="#28A745", fg="white", activebackground="#218838", activeforeground="white",
            font=("Segoe UI", 10, "bold"), padx=12, pady=6, cursor="hand2", relief="flat", bd=0,
        )
        self._vac_toggle_btn.pack(side="left", padx=(0, 6))
        add_hover_glow(self._vac_toggle_btn, "#28A745")
        connect_btn = tk.Button(
            btn_fr, text="Connect", command=self._connect_vacuum,
            bg="#3A3A50", fg="white", activebackground="#2E2E42", activeforeground="white",
            font=("Segoe UI", 10), padx=10, pady=6, cursor="hand2", relief="flat", bd=0,
        )
        connect_btn.pack(side="left")
        add_hover_glow(connect_btn, "#3A3A50")

        # Valve card
        valve_card = ttk.LabelFrame(bottom, text="Selector Valve (SV-07)", padding=10)
        valve_card.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        ttk.Label(valve_card, textvariable=self._valve_pos_var, font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(valve_card, textvariable=self._valve_status_var, foreground="#888").pack(anchor="w", pady=(2, 8))
        self._valve_port_grid = ttk.Frame(valve_card)
        self._valve_port_grid.pack(fill="x")
        self._build_valve_port_buttons()
        v_btn_fr = ttk.Frame(valve_card)
        v_btn_fr.pack(fill="x", pady=(8, 0))
        v_connect_btn = tk.Button(
            v_btn_fr, text="Connect Valve", command=self._connect_valve,
            bg="#3A3A50", fg="white", activebackground="#2E2E42", activeforeground="white",
            font=("Segoe UI", 10), padx=10, pady=6, cursor="hand2", relief="flat", bd=0,
        )
        v_connect_btn.pack(side="left", padx=(0, 6))
        add_hover_glow(v_connect_btn, "#3A3A50")

    # --- pump cards ---------------------------------------------------------

    def _build_pump_cards(self) -> None:
        for child in self._pumps_grid.winfo_children():
            child.destroy()
        self._pump_cards = []
        cols = self.PUMP_CARD_COLS
        for c in range(cols):
            self._pumps_grid.columnconfigure(c, weight=1, uniform="ovcard")
        for i, panel in enumerate(self.app.panels):
            r, c = divmod(i, cols)
            self._pump_cards.append(self._build_pump_card(self._pumps_grid, panel, r, c))

    def _build_pump_card(
        self, parent: ttk.Frame, panel: PumpPanel, row: int, col: int
    ) -> dict[str, Any]:
        title = f"Pump {panel.pump_index}"
        card = ttk.LabelFrame(parent, text=title, padding=8)
        card.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
        nick_var = tk.StringVar(value=panel.nickname_var.get().strip() or "(no nickname)")
        status_var = tk.StringVar(value="Not connected")
        live_var = tk.StringVar(value="—")
        ttk.Label(card, textvariable=nick_var, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(card, textvariable=status_var, foreground="#888").pack(anchor="w")
        ttk.Label(card, textvariable=live_var, font=("Segoe UI", 9), wraplength=240).pack(anchor="w", pady=(4, 8))

        btn_fr = ttk.Frame(card)
        btn_fr.pack(fill="x")
        run_btn = tk.Button(
            btn_fr, text="Run", command=lambda p=panel: self.app.run_with_mode(p),
            bg="#28A745", fg="white", activebackground="#218838", activeforeground="white",
            font=("Segoe UI", 9, "bold"), padx=8, pady=4, cursor="hand2", relief="flat", bd=0,
        )
        run_btn.pack(side="left", padx=(0, 4))
        add_hover_glow(run_btn, "#28A745")
        stop_btn = tk.Button(
            btn_fr, text="Stop", command=lambda p=panel: self.app.stop_with_mode(p),
            bg="#DC3545", fg="white", activebackground="#C82333", activeforeground="white",
            font=("Segoe UI", 9, "bold"), padx=8, pady=4, cursor="hand2", relief="flat", bd=0,
        )
        stop_btn.pack(side="left", padx=(0, 4))
        add_hover_glow(stop_btn, "#DC3545")
        switch_btn = tk.Button(
            btn_fr, text="\u2192 Valve",
            command=lambda i=panel.pump_index: self.app.switch_valve_to_pump(i),
            bg="#3B82F6", fg="white", activebackground="#1E40AF", activeforeground="white",
            font=("Segoe UI", 9, "bold"), padx=8, pady=4, cursor="hand2", relief="flat", bd=0,
        )
        switch_btn.pack(side="left")
        add_hover_glow(switch_btn, "#3B82F6")

        return {
            "panel": panel,
            "nick_var": nick_var,
            "status_var": status_var,
            "live_var": live_var,
        }

    # --- valve port buttons -------------------------------------------------

    def _build_valve_port_buttons(self) -> None:
        if self._valve_port_grid is None:
            return
        for child in self._valve_port_grid.winfo_children():
            child.destroy()
        self._valve_port_buttons = []
        vp = self.app.valve_panel
        try:
            n = int(vp.max_ports_var.get().strip()) if vp is not None else 6
        except ValueError:
            n = 6
        n = max(1, min(16, n))
        cols = 6
        for c in range(cols):
            self._valve_port_grid.columnconfigure(c, weight=1)
        for i in range(1, n + 1):
            btn = tk.Button(
                self._valve_port_grid,
                text=str(i),
                command=lambda p=i: self._move_valve(p),
                bg="#3A3A50", fg="white", font=("Segoe UI", 9, "bold"),
                relief="flat", bd=0, padx=4, pady=4, cursor="hand2",
            )
            r, c = divmod(i - 1, cols)
            btn.grid(row=r, column=c, padx=2, pady=2, sticky="ew")
            add_hover_glow(btn, "#3A3A50")
            self._valve_port_buttons.append(btn)

    def _highlight_valve_button(self, current: Optional[int]) -> None:
        for i, btn in enumerate(self._valve_port_buttons, start=1):
            try:
                btn.configure(bg="#28A745" if current == i else "#3A3A50")
            except tk.TclError:
                pass

    # --- public API used by the main app ------------------------------------

    def rebuild(self) -> None:
        self._build_pump_cards()
        self._build_valve_port_buttons()

    def refresh(self) -> None:
        # Per-pump: pull values directly from each PumpPanel's vars.
        for entry in self._pump_cards:
            panel: PumpPanel = entry["panel"]
            nick = panel.nickname_var.get().strip() or "(no nickname)"
            entry["nick_var"].set(nick)
            entry["status_var"].set(panel.status_var.get())
            live = panel.live_readout_var.get()
            entry["live_var"].set(live if live and live != "\u2014" else "—")

        # Vacuum
        vp = self.app.vacuum_panel
        if vp is not None:
            self._vac_bar_var.set(vp.bar_var.get() if hasattr(vp, "bar_var") else "Vacuum: --- bar")
            self._vac_status_var.set(vp.status_var.get() if hasattr(vp, "status_var") else "—")
            if self._vac_toggle_btn is not None:
                if getattr(vp, "is_on", False):
                    self._vac_toggle_btn.configure(text="Vacuum OFF", bg="#DC3545", activebackground="#C82333")
                else:
                    self._vac_toggle_btn.configure(text="Vacuum ON", bg="#28A745", activebackground="#218838")

        # Valve
        valve = self.app.valve_panel
        if valve is not None:
            self._valve_pos_var.set(valve.position_var.get())
            self._valve_status_var.set(valve.conn_status_var.get())
            try:
                cur_text = valve.position_var.get()
                if cur_text.startswith("Current port: "):
                    cur = int(cur_text.split(":", 1)[1].strip())
                    self._highlight_valve_button(cur)
                else:
                    self._highlight_valve_button(None)
            except (ValueError, IndexError):
                self._highlight_valve_button(None)

    # --- button handlers ----------------------------------------------------

    def _toggle_vacuum(self) -> None:
        vp = self.app.vacuum_panel
        if vp is not None:
            vp.toggle_vacuum()

    def _connect_vacuum(self) -> None:
        vp = self.app.vacuum_panel
        if vp is None:
            return
        if vp.serial_conn is not None and getattr(vp.serial_conn, "is_open", False):
            return
        vp.connect_arduino()

    def _connect_valve(self) -> None:
        valve = self.app.valve_panel
        if valve is None:
            return
        if valve.is_connected:
            return
        valve.connect_async()

    def _move_valve(self, port: int) -> None:
        valve = self.app.valve_panel
        if valve is None:
            return
        valve.move_to_port_async(port)


class PumpControllerApp(tk.Tk):
    PUMP_PANEL_WIDTH = 560
    PUMP_PANEL_HEIGHT = 458
    PUMPS_PER_ROW = 2

    def __init__(self) -> None:
        super().__init__()
        self.title("Syringe Pumps Control (NE-1000) + Vacuum + RUNZE SV-07")
        self.geometry("1280x900")
        self.minsize(1100, 760)

        (
            loaded_num_pumps,
            loaded_labels,
            loaded_port_map,
            loaded_port_labels,
        ) = load_pump_labels_file()
        self.num_pumps: int = loaded_num_pumps
        self._loaded_labels: dict[str, str] = loaded_labels
        # Live mapping used by the "Switch valve to this line" buttons and by
        # ``valve_to_pump`` recipe steps. Seeded from ``pump_labels.json`` and
        # overridden when a recipe is applied/run.
        self._active_pump_port_map: dict[int, int] = dict(loaded_port_map)
        self._saved_pump_port_map: dict[int, int] = dict(loaded_port_map)
        # Custom labels for valve ports that are NOT pumps — vents, bleeds,
        # waste, atmosphere, manual reservoirs, etc. Only one source: the
        # global pump_labels.json (no per-recipe override for now).
        self._active_port_labels: dict[int, str] = dict(loaded_port_labels)
        self._saved_port_labels: dict[int, str] = dict(loaded_port_labels)

        self.mode_var = tk.StringVar(value="Individual mode")
        self.switch_together_var = tk.BooleanVar(value=False)
        self.dark_mode = False
        self.panels: list[PumpPanel] = []
        self.vacuum_panel: Optional[VacuumPanel] = None
        self.valve_panel: Optional[ValvePanel] = None
        self.recipes_panel: Optional[RecipesPanel] = None
        self._recipes_window: Optional[tk.Toplevel] = None
        self._sequence_editor: Optional[RecipeSequenceEditor] = None
        self._save_pump_labels_job: Optional[str] = None
        self._quick_recipes_cache: list[dict[str, Any]] = []
        self._quick_recipe_var = tk.StringVar(value="")
        self._recipe_abort_event = threading.Event()
        self._recipe_thread_running = False
        self._active_recipe_name: str = ""
        self._active_recipe_from_toolbar: bool = False
        self._recipe_gui_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._recipe_gui_poll_after: Optional[str] = None
        self._abort_reinit_after: Optional[str] = None
        self._run_recipe_blink_after: Optional[str] = None
        self._quick_recipe_combo: Optional[ttk.Combobox] = None
        self._run_recipe_btn: Optional[tk.Button] = None
        self._abort_recipe_btn: Optional[ttk.Button] = None
        self._notebook: Optional[ttk.Notebook] = None
        self._pumps_canvas: Optional[tk.Canvas] = None
        self._pumps_inner: Optional[ttk.Frame] = None
        self._custom_inlet_bar: Optional[ttk.Frame] = None
        self._overview_tab: Optional["OverviewTab"] = None
        self._num_pumps_var = tk.StringVar(value=str(self.num_pumps))
        self._style = ttk.Style(self)
        self._style.theme_use("clam")
        self._build()
        self._schedule_live_updates()

    def _build(self) -> None:
        header = ttk.Frame(self, padding=(12, 12, 12, 6))
        header.pack(fill="x")
        self._header_frame = header
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
        ttk.Label(header, text="Pumps:").pack(side="left", padx=(16, 4))
        self._num_pumps_spin = ttk.Spinbox(
            header,
            from_=MIN_PUMPS,
            to=MAX_PUMPS,
            textvariable=self._num_pumps_var,
            width=4,
            command=self._on_num_pumps_spin,
        )
        self._num_pumps_spin.pack(side="left")
        self._num_pumps_var.trace_add("write", lambda *_a: self._on_num_pumps_var_change())
        self.dark_mode_btn = ttk.Button(header, text="Dark Mode", command=self.toggle_dark_mode)
        self.dark_mode_btn.pack(side="left", padx=(16, 0))

        power_btn = tk.Button(
            header, text="Power Off (Stop All)", command=self.power_off,
            cursor="hand2", padx=10, pady=4, relief="raised", bd=2,
            **BTN_POWER_OFF_ABORT,
        )
        power_btn.pack(side="right")
        add_hover_glow(power_btn, BTN_POWER_OFF_ABORT["bg"])
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
            cursor="hand2",
        )
        self._run_recipe_btn.pack(side="right", padx=(0, 6))
        add_hover_glow(self._run_recipe_btn, theme0["btn"])
        self._quick_recipe_combo = ttk.Combobox(
            header,
            textvariable=self._quick_recipe_var,
            values=[],
            state="readonly",
            width=28,
        )
        self._quick_recipe_combo.pack(side="right", padx=(0, 6))
        ttk.Label(header, text="Recipe:").pack(side="right", padx=(16, 4))

        ports = detected_port_names()

        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        # --- Overview tab (compact summary, default landing page) ----------
        overview_frame = ttk.Frame(self._notebook, padding=8)
        self._notebook.add(overview_frame, text="Overview")
        self._overview_tab = OverviewTab(overview_frame, self)
        self._overview_tab.pack(fill="both", expand=True)

        # --- Pumps tab (scrollable grid of PumpPanels) ---------------------
        pumps_frame = ttk.Frame(self._notebook, padding=4)
        self._notebook.add(pumps_frame, text="Pumps")
        self._build_pumps_tab(pumps_frame, ports)

        # --- Vacuum tab ----------------------------------------------------
        vacuum_frame = ttk.Frame(self._notebook, padding=8)
        self._notebook.add(vacuum_frame, text="Vacuum / Pressure")
        self.vacuum_panel = VacuumPanel(vacuum_frame, ports)
        self.vacuum_panel.pack(fill="both", expand=True)

        # --- Selector Valve tab --------------------------------------------
        valve_frame = ttk.Frame(self._notebook, padding=8)
        self._notebook.add(valve_frame, text="Selector Valve")
        self.valve_panel = ValvePanel(valve_frame, self, ports)
        self.valve_panel.pack(fill="both", expand=True)

        # Apply loaded nicknames to pump panels (loaded earlier in __init__).
        for i, panel in enumerate(self.panels):
            panel.nickname_var.set(self._loaded_labels.get(str(i + 1), ""))
            panel._refresh_frame_title()

        # Overview is constructed before PumpPanels exist; populate its pump cards once panels are ready.
        if self._overview_tab is not None:
            try:
                self._overview_tab.rebuild()
            except tk.TclError:
                pass

        bottom_bar = ttk.Frame(self, padding=(12, 0, 12, 8))
        bottom_bar.pack(fill="x", side="bottom")
        self._tip_label = ttk.Label(
            bottom_bar,
            text="Tip: type COM ports manually (e.g. COM3) even without hardware connected.",
        )
        self._tip_label.pack(side="left", anchor="w")

        self._progress_frame = ttk.Frame(bottom_bar)
        self._progress_frame.pack(side="right")
        self._progress_label = ttk.Label(self._progress_frame, text="")
        self._progress_label.pack(side="top", anchor="e")
        self._progress_bar = ttk.Progressbar(self._progress_frame, length=260, mode="determinate")
        self._progress_bar.pack(side="top", anchor="e", pady=(2, 0))
        self._progress_frame.pack_forget()
        self._progress_start_time: float = 0.0
        self._progress_total_time: float = 0.0
        self._progress_tick_id: Optional[str] = None
        self._recipe_progress_pause_accum: float = 0.0

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_quick_recipe_combo()
        self._notebook.select(overview_frame)

    def _build_pumps_tab(self, parent: ttk.Frame, ports: list[str]) -> None:
        """Build a vertically scrollable grid container that holds N PumpPanels."""
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)
        # Row 0 = custom-inlets bar (sized to its content), row 1 = scrollable pumps area.
        outer.rowconfigure(0, weight=0)
        outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)

        # --- Custom inlets quick-access bar (shown when port_labels is non-empty) -----
        self._custom_inlet_bar = ttk.Frame(outer, padding=(4, 4, 4, 6))
        self._custom_inlet_bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        # Buttons are populated lazily in _rebuild_custom_inlet_bar() so the
        # frame is initially empty and shows nothing if no labels are set.

        theme = DARK if self.dark_mode else LIGHT
        canvas = tk.Canvas(outer, highlightthickness=0, bg=theme["bg"], borderwidth=0)
        canvas.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        sb.grid(row=1, column=1, sticky="ns")
        canvas.configure(yscrollcommand=sb.set)

        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def on_inner_configure(_e: Any = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def on_canvas_configure(event: tk.Event) -> None:
            canvas.itemconfigure(inner_id, width=event.width)

        inner.bind("<Configure>", on_inner_configure)
        canvas.bind("<Configure>", on_canvas_configure)

        def _on_mousewheel(event: tk.Event) -> None:
            try:
                canvas.yview_scroll(int(-event.delta / 120), "units")
            except tk.TclError:
                pass

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        self._pumps_canvas = canvas
        self._pumps_inner = inner

        for i in range(self.num_pumps):
            self.panels.append(PumpPanel(inner, self, i + 1, ports))
        self._layout_pump_panels()
        # Bar starts empty; gets populated as the user adds labels (via set_port_labels).
        self._rebuild_custom_inlet_bar()

    def _rebuild_custom_inlet_bar(self) -> None:
        """Re-render the Custom inlets quick-access bar from ``_active_port_labels``."""
        bar = getattr(self, "_custom_inlet_bar", None)
        if bar is None:
            return
        for child in bar.winfo_children():
            child.destroy()
        labels = self._active_port_labels
        if not labels:
            ttk.Label(
                bar,
                text=("Custom inlets: none defined yet — set port labels on the "
                      "Selector Valve tab to get one-click vent / waste / bleed buttons here."),
                foreground="#888888",
            ).pack(side="left")
            return
        ttk.Label(bar, text="Custom inlets:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(2, 8))
        for port in sorted(labels):
            text = f"{labels[port]}  →  port {port}"
            btn = tk.Button(
                bar,
                text=text,
                command=lambda p=port: self._on_custom_inlet_click(p),
                bg="#3B82F6",
                fg="white",
                activebackground="#1E40AF",
                activeforeground="white",
                font=("Segoe UI", 9, "bold"),
                padx=10,
                pady=4,
                cursor="hand2",
                relief="flat",
                bd=0,
            )
            btn.pack(side="left", padx=(0, 6))
            add_hover_glow(btn, "#3B82F6")

    def _on_custom_inlet_click(self, port: int) -> None:
        """Move the valve to a custom-inlet port; surface a friendly popup if not connected."""
        vp = self.valve_panel
        if vp is None:
            return
        if not vp.is_connected:
            messagebox.showinfo(
                "Switch valve",
                "The selector valve is not connected. Open the Selector Valve tab and "
                "click Connect Valve, then try again.",
                parent=self,
            )
            return
        vp.move_to_port_async(port)

    def _layout_pump_panels(self) -> None:
        if self._pumps_inner is None:
            return
        for child in self._pumps_inner.grid_slaves():
            child.grid_forget()
        cols = self.PUMPS_PER_ROW
        for c in range(cols):
            self._pumps_inner.columnconfigure(c, weight=1, uniform="ppanel")
        for idx, panel in enumerate(self.panels):
            r, c = divmod(idx, cols)
            panel.configure(width=self.PUMP_PANEL_WIDTH, height=self.PUMP_PANEL_HEIGHT)
            panel.grid_propagate(False)
            panel.grid(row=r, column=c, sticky="nsew", padx=6, pady=6)
        if self._pumps_canvas is not None:
            self._pumps_canvas.update_idletasks()
            self._pumps_canvas.configure(scrollregion=self._pumps_canvas.bbox("all"))

    def _on_num_pumps_spin(self) -> None:
        self._on_num_pumps_var_change()

    def _on_num_pumps_var_change(self) -> None:
        try:
            n = int(self._num_pumps_var.get().strip())
        except ValueError:
            return
        n = max(MIN_PUMPS, min(MAX_PUMPS, n))
        if n == self.num_pumps:
            return
        self.set_num_pumps(n)

    def set_num_pumps(self, new_n: int) -> None:
        new_n = max(MIN_PUMPS, min(MAX_PUMPS, new_n))
        if new_n == self.num_pumps:
            return
        if self._pumps_inner is None:
            self.num_pumps = new_n
            return
        ports = detected_port_names()
        if new_n > self.num_pumps:
            for i in range(self.num_pumps, new_n):
                self.panels.append(PumpPanel(self._pumps_inner, self, i + 1, ports))
        else:
            for panel in self.panels[new_n:]:
                try:
                    if panel.connection.pump is not None:
                        panel.disconnect_sync()
                except Exception:
                    pass
                try:
                    panel.destroy()
                except tk.TclError:
                    pass
            self.panels = self.panels[:new_n]
        self.num_pumps = new_n
        self._num_pumps_var.set(str(new_n))
        self._layout_pump_panels()
        if self._overview_tab is not None:
            try:
                self._overview_tab.rebuild()
            except tk.TclError:
                pass
        if self.valve_panel is not None:
            try:
                self.valve_panel.refresh_mapping_table()
            except tk.TclError:
                pass
        self.schedule_save_pump_labels()

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

    def _show_progress_bar(self, total_seconds: float, *, unknown_duration: bool = False) -> None:
        self._recipe_progress_pause_accum = 0.0
        self._progress_unknown_duration = unknown_duration
        self._progress_total_time = max(total_seconds, 0.1)
        self._progress_start_time = time.time()
        self._progress_bar.configure(maximum=100, value=0)
        if unknown_duration:
            self._progress_label.configure(
                text="Recipe running \u2014 0% (approximate timeline; valve checks pause the bar)",
            )
        else:
            self._progress_label.configure(text=f"Recipe running \u2014 0% (est. {_format_duration(total_seconds)})")
        self._progress_frame.pack(side="right")
        self._progress_tick()

    def _progress_tick(self) -> None:
        if not self._recipe_thread_running:
            return
        elapsed = max(
            0.0,
            time.time() - self._progress_start_time - self._recipe_progress_pause_accum,
        )
        pct = min(100.0, (elapsed / self._progress_total_time) * 100.0)
        remaining = max(0.0, self._progress_total_time - elapsed)
        self._progress_bar.configure(value=pct)
        if getattr(self, "_progress_unknown_duration", False):
            self._progress_label.configure(text=f"Recipe running \u2014 {pct:.0f}%  (approx.)")
        else:
            self._progress_label.configure(
                text=f"Recipe running \u2014 {pct:.0f}%  ({_format_duration(remaining)} remaining)",
            )
        self._progress_tick_id = self.after(500, self._progress_tick)

    def _refresh_recipe_progress_after_checkpoint(self) -> None:
        """Resync bar/label after a blocking valve checkpoint (main thread only)."""
        if not self._recipe_thread_running:
            return
        if self._progress_tick_id is not None:
            try:
                self.after_cancel(self._progress_tick_id)
            except tk.TclError:
                pass
            self._progress_tick_id = None
        self._progress_tick()

    def _hide_progress_bar(self) -> None:
        if self._progress_tick_id is not None:
            try:
                self.after_cancel(self._progress_tick_id)
            except tk.TclError:
                pass
            self._progress_tick_id = None
        self._recipe_progress_pause_accum = 0.0
        self._progress_frame.pack_forget()

    def _preflight_check(self, recipe: dict[str, Any]) -> Optional[str]:
        """Return an error string if the recipe can't run, or None if OK."""
        if self._recipe_thread_running:
            return "A recipe is already running. Abort it first or wait until it finishes."
        return None

    def _confirm_and_run_recipe(self, recipe: dict[str, Any], *, from_toolbar: bool = False) -> None:
        """Show a confirmation dialog with recipe summary, then run."""
        err = self._preflight_check(recipe)
        if err:
            messagebox.showwarning("Cannot Run Recipe", err, parent=self)
            return

        name = recipe.get("name", "(untitled)")
        steps = recipe.get("steps")
        is_sequence = isinstance(steps, list) and len(steps) > 0
        est_time = _estimate_recipe_time(recipe) if is_sequence else 0.0
        pumps_used, vacuum_needed, valve_needed = _recipe_resources(recipe)
        nicks = self.pump_nickname_map()

        theme = DARK if self.dark_mode else LIGHT
        d = tk.Toplevel(self)
        d.title(f"Run recipe — {name}")
        d.transient(self)
        d.grab_set()
        d.resizable(True, True)
        d.configure(bg=theme["bg"])

        outer = ttk.Frame(d, padding=20)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text=name, font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text=f"Estimated time: {_format_duration(est_time)}" if is_sequence else "Simple recipe (apply + run connected pumps)",
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(2, 10))

        cards = ttk.Frame(outer)
        cards.pack(fill="x", pady=(0, 8))
        for c in range(4):
            cards.columnconfigure(c, weight=1, uniform="card")

        def _card(parent: ttk.Frame, col: int, title_text: str, body_text: str) -> None:
            fr = ttk.LabelFrame(parent, text=title_text, padding=8)
            fr.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 4, 0))
            ttk.Label(fr, text=body_text, wraplength=160).pack(anchor="w")

        pump_desc = ", ".join(
            f"Pump {p}" + (f" ({nicks[p]})" if p in nicks else "")
            for p in sorted(pumps_used)
        ) if pumps_used else "(none detected)"
        _card(cards, 0, "PUMPS", pump_desc)
        _card(cards, 1, "VACUUM", "Required" if vacuum_needed else "Not used")
        _card(cards, 2, "VALVE", "Required" if valve_needed else "Not used")
        _card(cards, 3, "STEPS", f"{len(steps)} steps" if is_sequence else "Apply + Run")

        if is_sequence:
            ttk.Label(outer, text="Steps:", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(4, 2))
            steps_fr = ttk.Frame(outer)
            steps_fr.pack(fill="both", expand=True, pady=(0, 8))
            steps_fr.rowconfigure(0, weight=1)
            steps_fr.columnconfigure(0, weight=1)
            sl = tk.Listbox(
                steps_fr, height=min(12, max(4, len(steps))),
                bg=theme["input"], fg=theme["fg"],
                selectbackground=theme["btn"], selectforeground=theme["fg"],
                highlightthickness=0, borderwidth=1, relief="solid",
            )
            sl.grid(row=0, column=0, sticky="nsew")
            ssb = ttk.Scrollbar(steps_fr, orient="vertical", command=sl.yview)
            ssb.grid(row=0, column=1, sticky="ns")
            sl.configure(yscrollcommand=ssb.set)
            for i, st in enumerate(steps):
                if isinstance(st, dict):
                    sl.insert(tk.END, format_step_list_line(i, st, nicks))

        confirmed = [False]

        def on_confirm() -> None:
            confirmed[0] = True
            d.destroy()

        btn_fr = ttk.Frame(outer)
        btn_fr.pack(fill="x", pady=(8, 0))
        ttk.Button(btn_fr, text="Cancel", command=d.destroy).pack(side="left")
        confirm_btn = tk.Button(
            btn_fr, text="Confirm & Run", command=on_confirm,
            bg="#28A745", fg="white", activebackground="#218838", activeforeground="white",
            font=("Segoe UI", 10, "bold"), padx=16, pady=4, cursor="hand2",
            relief="flat", bd=0,
        )
        confirm_btn.pack(side="right")
        add_hover_glow(confirm_btn, "#28A745")

        d.update_idletasks()
        w = max(d.winfo_reqwidth(), 520)
        h = max(d.winfo_reqheight(), 350)
        px = self.winfo_rootx() + (self.winfo_width() - w) // 2
        py = self.winfo_rooty() + (self.winfo_height() - h) // 2
        d.geometry(f"{w}x{h}+{px}+{py}")
        d.wait_window()

        if not confirmed[0]:
            return

        if is_sequence:
            self.run_recipe_sequence(recipe, from_toolbar=from_toolbar)
        else:
            self.run_recipe(recipe, from_toolbar=from_toolbar)

    def _on_toolbar_run_recipe(self) -> None:
        if self._recipe_thread_running:
            messagebox.showwarning("Recipe", "A recipe is already running. Use \"Abort recipe\" or wait until it finishes.", parent=self)
            return
        recipes = self._quick_recipes_cache
        if not recipes:
            messagebox.showinfo("Recipe", "No saved recipes yet. Open \"Recipes\" and use \"Save from main window\".", parent=self)
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
        self._confirm_and_run_recipe(rec, from_toolbar=True)

    def schedule_gui(self, fn: Callable[[], None]) -> None:
        """Queue *fn* to run on the Tk main thread (safe from recipe worker threads)."""
        self._recipe_gui_queue.put(fn)

    def _drain_and_stop_recipe_gui_queue(self) -> None:
        if self._recipe_gui_poll_after is not None:
            try:
                self.after_cancel(self._recipe_gui_poll_after)
            except tk.TclError:
                pass
            self._recipe_gui_poll_after = None
        while True:
            try:
                self._recipe_gui_queue.get_nowait()
            except queue.Empty:
                break

    def _recipe_gui_poll_tick(self) -> None:
        self._recipe_gui_poll_after = None
        try:
            while True:
                fn = self._recipe_gui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        if self._recipe_thread_running:
            self._recipe_gui_poll_after = self.after(20, self._recipe_gui_poll_tick)

    def _start_recipe_gui_poll(self) -> None:
        self._drain_and_stop_recipe_gui_queue()
        self._recipe_gui_poll_tick()

    def abort_recipe_run(self) -> None:
        """Stop the recipe UI immediately, signal the worker, stop pumps/vacuum/valve."""
        if not self._recipe_thread_running:
            return
        self._recipe_abort_event.set()
        name = self._active_recipe_name
        ftb = self._active_recipe_from_toolbar
        vp = self.vacuum_panel
        if vp is not None:
            vp._stop_vacuum_blink()
            vp.is_on = False
            try:
                vp.toggle_btn.config(text="Tap ON")
            except tk.TclError:
                pass
            try:
                vp._set_button_color()
            except tk.TclError:
                pass
            vp._set_status("Vacuum OFF (recipe aborted)")
        valve = self.valve_panel
        if valve is not None:
            try:
                valve._set_motion("Recipe aborted")
            except tk.TclError:
                pass
        self._finish_recipe_run(ftb, name, user_aborted=True, had_error=False)
        threading.Thread(target=self._abort_stop_hardware_motion, daemon=True).start()
        if self._abort_reinit_after is not None:
            try:
                self.after_cancel(self._abort_reinit_after)
            except tk.TclError:
                pass
            self._abort_reinit_after = None
        self._abort_reinit_after = self.after(200, self._reinitialize_after_abort)

    def _abort_stop_hardware_motion(self) -> None:
        """Best-effort stop pumps, vacuum, and valve motion (runs on a background thread)."""
        for panel in self.panels:
            if panel.connection.pump is None:
                continue
            try:
                panel.stop_sync()
            except Exception:
                pass
        vp = self.vacuum_panel
        if vp is not None:
            try:
                with vp._serial_lock:
                    conn = vp.serial_conn
                    if conn is not None and getattr(conn, "is_open", False):
                        try:
                            conn.write(b"0")
                            conn.flush()
                        except Exception:
                            pass
                    vp.is_on = False
            except Exception:
                pass
        valve = self.valve_panel
        if valve is not None and valve.driver is not None and valve.driver.is_open:
            try:
                with valve._serial_lock:
                    valve.driver.force_stop()
            except Exception:
                pass

    def _reinitialize_after_abort(self) -> None:
        """Disconnect pumps, vacuum, and valve on the main thread after an abort."""
        self._abort_reinit_after = None
        if self._recipe_thread_running:
            return
        for panel in self.panels:
            try:
                panel.disconnect_sync()
            except Exception:
                pass
            try:
                panel.set_status("Aborted — use Connect or Reinitialize")
            except tk.TclError:
                pass
        if self.vacuum_panel is not None:
            try:
                self.vacuum_panel.close_serial_sync()
            except Exception:
                pass
        if self.valve_panel is not None:
            try:
                self.valve_panel.close_serial_sync()
            except Exception:
                pass

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

    def _finish_recipe_run(
        self,
        from_toolbar: bool,
        recipe_name: str = "",
        *,
        user_aborted: bool = False,
        had_error: bool = False,
    ) -> None:
        self._drain_and_stop_recipe_gui_queue()
        self._recipe_thread_running = False
        self._active_recipe_name = ""
        # Abort flag is cleared only when a new recipe starts (_begin_recipe_run) so the worker
        # can still see abort.is_set() until it exits after a toolbar abort.
        if from_toolbar:
            self._stop_run_recipe_blink()
        if self._abort_recipe_btn is not None and self._abort_recipe_btn.winfo_exists():
            self._abort_recipe_btn.configure(state="disabled")
        self._hide_progress_bar()
        if recipe_name:
            if had_error:
                pass
            elif user_aborted:
                messagebox.showinfo(
                    "Recipe aborted",
                    f'Recipe "{recipe_name}" was stopped.',
                    parent=self,
                )
            else:
                messagebox.showinfo("Recipe Complete", f'Recipe "{recipe_name}" Complete', parent=self)

    def _begin_recipe_run(self, from_toolbar: bool) -> None:
        if self._abort_reinit_after is not None:
            try:
                self.after_cancel(self._abort_reinit_after)
            except tk.TclError:
                pass
            self._abort_reinit_after = None
        self._recipe_abort_event.clear()
        self._recipe_thread_running = True
        self._start_recipe_gui_poll()
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
        save_pump_labels_file(
            self.num_pumps,
            {str(i + 1): p.nickname_var.get().strip() for i, p in enumerate(self.panels)},
            pump_port_map=self._saved_pump_port_map,
            port_labels=self._saved_port_labels,
        )

    def pump_nickname_map(self) -> dict[int, str]:
        return {
            i + 1: p.nickname_var.get().strip()
            for i, p in enumerate(self.panels)
            if p.nickname_var.get().strip()
        }

    def set_pump_port_mapping(
        self,
        new_map: dict[int, int],
        *,
        persist: bool = True,
    ) -> None:
        """Update the live pump→valve-port mapping.

        When ``persist`` is True (default), also remember it as the saved default
        in ``pump_labels.json`` so it survives restarts. Recipe-driven calls pass
        ``persist=False`` so a recipe override doesn't clobber the user's
        global mapping.
        """
        cleaned = _coerce_pump_port_map(new_map)
        self._active_pump_port_map = dict(cleaned)
        if persist:
            self._saved_pump_port_map = dict(cleaned)
            self.schedule_save_pump_labels()
        if self.valve_panel is not None:
            try:
                self.valve_panel.refresh_mapping_table()
                # Hints in the labels table show "(Pump N)" next to each port
                # if a pump is mapped there — keep them current.
                self.valve_panel.refresh_label_table()
                self.valve_panel.refresh_port_button_text()
            except tk.TclError:
                pass

    def set_port_labels(
        self,
        new_labels: dict[int, str],
        *,
        persist: bool = True,
    ) -> None:
        """Update the live custom port labels (vents/bleeds/waste/etc.).

        ``persist=True`` writes them to ``pump_labels.json`` (the labels are
        considered physical-wiring info, so there's no recipe override path).
        """
        cleaned = _coerce_port_labels(new_labels)
        self._active_port_labels = dict(cleaned)
        if persist:
            self._saved_port_labels = dict(cleaned)
            self.schedule_save_pump_labels()
        if self.valve_panel is not None:
            try:
                self.valve_panel.refresh_label_table()
                self.valve_panel.refresh_port_button_text()
            except tk.TclError:
                pass
        try:
            self._rebuild_custom_inlet_bar()
        except tk.TclError:
            pass

    def port_assignment_text(self, port: int) -> str:
        """Return a short label describing what is wired to ``port``.

        Pump mapping wins over a custom label; if neither, returns ``""``.
        """
        for pump_index, mapped_port in self._active_pump_port_map.items():
            if mapped_port == port:
                if 1 <= pump_index <= len(self.panels):
                    nick = self.panels[pump_index - 1].nickname_var.get().strip()
                    if nick:
                        return nick
                return f"Pump {pump_index}"
        return self._active_port_labels.get(port, "")

    def valve_connection_snapshot(self) -> Optional[dict[str, Any]]:
        """Return the current valve panel's connection settings, or ``None`` if no panel."""
        vp = self.valve_panel
        if vp is None:
            return None
        com = vp.com_var.get().strip()
        if not com:
            return None
        try:
            baud = int(vp.baud_var.get().strip())
        except ValueError:
            baud = 9600
        try:
            addr = int(vp.addr_var.get().strip())
        except ValueError:
            addr = 0
        try:
            mp = int(vp.max_ports_var.get().strip())
        except ValueError:
            mp = 6
        return {"com": com, "baud": baud, "address": addr, "max_ports": mp}

    def apply_recipe_to_panels(self, recipe: dict[str, Any]) -> None:
        """Copy saved COM/baud/address, per-pump settings, valve config, and port mapping."""
        target_n = recipe_num_pumps(recipe)
        if target_n != self.num_pumps:
            self.set_num_pumps(target_n)

        for i, panel in enumerate(self.panels):
            conn = recipe_pump_conn(recipe, i + 1)
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
            data = recipe_pump_settings(recipe, i + 1)
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

        vc = recipe_valve_conn(recipe)
        if isinstance(vc, dict) and self.valve_panel is not None:
            com = str(vc.get("com", "")).strip()
            if com:
                self.valve_panel.com_var.set(com)
            try:
                baud = int(vc.get("baud", 9600))
                self.valve_panel.baud_var.set(str(baud))
            except (TypeError, ValueError):
                pass
            try:
                addr = int(vc.get("address", 0))
                self.valve_panel.addr_var.set(str(addr))
            except (TypeError, ValueError):
                pass
            try:
                mp = int(vc.get("max_ports", 6))
                self.valve_panel.max_ports_var.set(str(mp))
                self.valve_panel._rebuild_port_grid()
            except (TypeError, ValueError):
                pass

        rp_map = recipe_pump_port_map(recipe)
        if rp_map:
            # A recipe override should drive the live "Switch valve" buttons but
            # not silently overwrite the user's saved global mapping.
            self.set_pump_port_mapping(rp_map, persist=False)
        elif self.valve_panel is not None:
            # Fall back to the persisted global mapping so the table never goes blank.
            self.set_pump_port_mapping(self._saved_pump_port_map, persist=False)

    def run_recipe_sequence(self, recipe: dict[str, Any], *, from_toolbar: bool = False) -> None:
        """Execute ordered steps (delays, pump connect/apply/run, vacuum) on a background thread."""
        steps = recipe.get("steps")
        if not isinstance(steps, list) or not steps:
            return
        if self._recipe_thread_running:
            messagebox.showwarning("Recipe", "A recipe is already running. Abort it first or wait until it finishes.", parent=self)
            return

        # Make sure the panel grid matches the recipe's expected pump count *before* we run.
        # This way per-recipe pump-port mappings line up with the actual pumps the user is
        # about to use.
        target_n = recipe_num_pumps(recipe)
        if target_n != self.num_pumps:
            self.set_num_pumps(target_n)

        # Pre-load the per-recipe pump-port mapping so valve_to_pump steps and
        # the "Switch valve" buttons share the same source of truth during this run.
        rp_map = recipe_pump_port_map(recipe)
        if rp_map:
            self.set_pump_port_mapping(rp_map, persist=False)
        else:
            # No recipe-specific mapping → use the persisted global default so
            # users don't have to re-enter ports for every recipe.
            self.set_pump_port_mapping(self._saved_pump_port_map, persist=False)
        port_map_snapshot = dict(self._active_pump_port_map)

        recipe_name = recipe.get("name", "")
        est = _estimate_recipe_time(recipe)
        self._active_recipe_name = recipe_name
        self._active_recipe_from_toolbar = from_toolbar
        self._begin_recipe_run(from_toolbar)
        # Always show the bar for sequences (toolbar + Recipes window) so valve checkpoints can pause the clock.
        if est > 0:
            self._show_progress_bar(est)
        else:
            self._show_progress_bar(180.0, unknown_duration=True)
        abort = self._recipe_abort_event
        app = self
        nicks = self.pump_nickname_map()
        max_pumps_now = len(self.panels)

        def worker() -> None:
            had_error = False
            was_aborted = False
            try:
                vac = app.vacuum_panel
                valve = app.valve_panel
                for idx, step in enumerate(steps):
                    if abort.is_set():
                        was_aborted = True
                        return
                    if not isinstance(step, dict):
                        continue
                    st = step.get("type")
                    try:
                        if st == "delay":
                            abortable_sleep(max(0.0, float(step.get("seconds", 0))), abort)
                            if abort.is_set():
                                was_aborted = True
                                return
                        elif st == "connect_pump":
                            p = int(step["pump"])
                            if p < 1 or p > max_pumps_now:
                                raise ValueError(f"Invalid pump {p} (1..{max_pumps_now} available)")
                            panel = app.panels[p - 1]
                            com = str(step["com"]).strip()
                            baud = int(step["baud"])
                            addr = int(step["address"])

                            def set_p_fields() -> None:
                                panel.com_var.set(com)
                                panel.baud_var.set(str(baud))
                                panel.address_var.set(str(addr))

                            if abort.is_set():
                                was_aborted = True
                                return
                            run_on_main_thread_abortable(app, set_p_fields, abort)
                            if abort.is_set():
                                was_aborted = True
                                return
                            panel.connect_with_params(com, baud, addr)
                        elif st == "disconnect_pump":
                            p = int(step["pump"])
                            if 1 <= p <= max_pumps_now:
                                app.panels[p - 1].disconnect_sync()
                        elif st == "apply_pump":
                            p = int(step["pump"])
                            if p < 1 or p > max_pumps_now:
                                raise ValueError(f"Invalid pump {p}")
                            panel = app.panels[p - 1]
                            settings = step.get("settings")
                            if not isinstance(settings, dict):
                                raise ValueError("apply_pump needs a settings object")

                            def apply_u() -> None:
                                panel.apply_settings_from_snapshot(settings)

                            if abort.is_set():
                                was_aborted = True
                                return
                            run_on_main_thread_abortable(app, apply_u, abort)
                            if abort.is_set():
                                was_aborted = True
                                return
                            panel.apply_settings_sync()
                        elif st == "line_check":
                            p = int(step["pump"])
                            if p < 1 or p > max_pumps_now:
                                raise ValueError(f"Invalid pump {p}")
                            if abort.is_set():
                                was_aborted = True
                                return
                            if not recipe_confirm_pump_line_open(app, p, nicks, abort):
                                was_aborted = True
                                return
                            if abort.is_set():
                                was_aborted = True
                                return
                        elif st == "run_pump":
                            p = int(step["pump"])
                            if p < 1 or p > max_pumps_now:
                                raise ValueError(f"Invalid pump {p}")
                            panel = app.panels[p - 1]
                            if abort.is_set():
                                was_aborted = True
                                return
                            panel.apply_settings_sync()
                            if abort.is_set():
                                was_aborted = True
                                return
                            if not panel.run_sync_for_recipe(abort):
                                was_aborted = True
                                return
                        elif st == "stop_pump":
                            p = int(step["pump"])
                            if 1 <= p <= max_pumps_now:
                                app.panels[p - 1].stop_sync()
                        elif st == "vacuum_connect":
                            if vac is None:
                                raise RuntimeError("Vacuum panel not available")
                            com = str(step["com"]).strip()
                            baud = int(step.get("baud", 9600))

                            def set_v() -> None:
                                vac.com_var.set(com)

                            if abort.is_set():
                                was_aborted = True
                                return
                            run_on_main_thread_abortable(app, set_v, abort)
                            if abort.is_set():
                                was_aborted = True
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
                        elif st == "valve_connect":
                            if valve is None:
                                raise RuntimeError("Valve panel not available")
                            com = str(step["com"]).strip()
                            baud = int(step.get("baud", 9600))
                            addr = int(step.get("address", 0))
                            mp_raw = step.get("max_ports")
                            mp = int(mp_raw) if mp_raw is not None else None
                            if abort.is_set():
                                was_aborted = True
                                return
                            valve.open_serial_explicit(com, baud=baud, address=addr, max_ports=mp)
                        elif st == "valve_disconnect":
                            if valve is not None:
                                valve.close_serial_sync()
                        elif st == "valve_to_port":
                            if valve is None:
                                raise RuntimeError("Valve panel not available — add a 'Valve connect' step.")
                            port = int(step["port"])
                            if abort.is_set():
                                was_aborted = True
                                return
                            result = valve.move_to_port_sync(port, abort=abort)
                            if result < 0:
                                was_aborted = True
                                return
                        elif st == "valve_to_pump":
                            if valve is None:
                                raise RuntimeError("Valve panel not available — add a 'Valve connect' step.")
                            p = int(step["pump"])
                            port = port_map_snapshot.get(p)
                            if port is None:
                                raise RuntimeError(
                                    f"No pump-to-port mapping for pump {p}. Open the recipe's "
                                    f"sequence editor and use 'Pump↔Port mapping…' to set it."
                                )
                            if abort.is_set():
                                was_aborted = True
                                return
                            result = valve.move_to_port_sync(port, abort=abort)
                            if result < 0:
                                was_aborted = True
                                return
                        elif st == "valve_to_label":
                            if valve is None:
                                raise RuntimeError("Valve panel not available — add a 'Valve connect' step.")
                            wanted = str(step.get("label", "")).strip()
                            if not wanted:
                                raise RuntimeError("valve_to_label step has an empty label.")
                            # Resolve label → port at run time so the latest custom labels apply.
                            wanted_lc = wanted.casefold()
                            port = None
                            for cand_port, cand_label in app._active_port_labels.items():
                                if cand_label.strip().casefold() == wanted_lc:
                                    port = cand_port
                                    break
                            if port is None:
                                raise RuntimeError(
                                    f"No port labelled '{wanted}'. Open the Selector Valve "
                                    f"tab and add it under 'Custom port labels'."
                                )
                            if abort.is_set():
                                was_aborted = True
                                return
                            result = valve.move_to_port_sync(port, abort=abort)
                            if result < 0:
                                was_aborted = True
                                return
                        elif st == "disconnect_everything":
                            for pi in range(max_pumps_now):
                                if abort.is_set():
                                    was_aborted = True
                                    return
                                panel = app.panels[pi]
                                if panel.connection.pump is not None:
                                    try:
                                        panel.stop_sync()
                                    except Exception:
                                        pass
                            if vac is not None:
                                if abort.is_set():
                                    was_aborted = True
                                    return
                                try:
                                    vac.send_vacuum_sync(False)
                                except Exception:
                                    pass
                                try:
                                    vac.close_serial_sync()
                                except Exception:
                                    pass
                            if valve is not None:
                                if abort.is_set():
                                    was_aborted = True
                                    return
                                try:
                                    valve.close_serial_sync()
                                except Exception:
                                    pass
                            if abort.is_set():
                                was_aborted = True
                                return
                            for pi in range(max_pumps_now):
                                try:
                                    app.panels[pi].disconnect_sync()
                                except Exception:
                                    pass
                        else:
                            raise ValueError(f"Unknown step type: {st}")
                    except RecipeAbort:
                        was_aborted = True
                        return
                    except Exception as exc:
                        had_error = True
                        detail = _friendly_step_error(step, idx, str(exc), nicks)
                        app.schedule_gui(
                            lambda d=detail: messagebox.showerror("Recipe Failed", d, parent=app),
                        )
                        return
            finally:
                def _fin_seq() -> None:
                    if not app._recipe_thread_running:
                        return
                    app._finish_recipe_run(
                        from_toolbar,
                        recipe_name,
                        user_aborted=was_aborted,
                        had_error=had_error,
                    )

                app.schedule_gui(_fin_seq)

        threading.Thread(target=worker, daemon=True).start()

    def run_recipe(self, recipe: dict[str, Any], *, from_toolbar: bool = False) -> None:
        """Apply recipe to panel fields, then apply+run each pump that is connected."""
        if self._recipe_thread_running:
            messagebox.showwarning("Recipe", "A recipe is already running. Abort it first or wait until it finishes.", parent=self)
            return

        recipe_name = recipe.get("name", "")
        self._active_recipe_name = recipe_name
        self._active_recipe_from_toolbar = from_toolbar
        self._begin_recipe_run(from_toolbar)
        self.apply_recipe_to_panels(recipe)
        est_simple = _estimate_recipe_time(recipe)
        if est_simple > 0:
            self._show_progress_bar(est_simple)
        else:
            self._show_progress_bar(180.0, unknown_duration=True)
        abort = self._recipe_abort_event
        app = self

        def work() -> None:
            had_error = False
            was_aborted = False
            any_ran = False
            try:
                for panel in app.panels:
                    if abort.is_set():
                        was_aborted = True
                        return
                    if panel.connection.pump is None:
                        app.schedule_gui(
                            lambda p=panel: p.set_status("Recipe: not connected \u2014 skipped"),
                        )
                        continue
                    try:
                        panel.apply_settings_sync()
                        if abort.is_set():
                            was_aborted = True
                            return
                        if not panel.run_sync_for_recipe(abort):
                            was_aborted = True
                            return
                        any_ran = True
                        app.schedule_gui(
                            lambda p=panel: p.set_status(
                                "Recipe: volume dispense finished"
                                if p.dispense_mode_var.get() == "Volume"
                                else "Running (recipe)"
                            ),
                        )
                    except Exception as exc:
                        had_error = True
                        msg = str(exc)
                        app.schedule_gui(lambda p=panel, m=msg: p.set_status(f"Recipe error: {m}"))
                if not any_ran and not had_error and not was_aborted:
                    app.schedule_gui(
                        lambda rn=recipe_name: messagebox.showerror(
                            "Recipe Failed",
                            f'Recipe "{rn}" could not run.\n\nNo pumps were connected. Connect the required pumps and try again.',
                            parent=app,
                        ),
                    )
                    had_error = True
            finally:
                def _fin_simple() -> None:
                    if not app._recipe_thread_running:
                        return
                    app._finish_recipe_run(
                        from_toolbar,
                        recipe_name,
                        user_aborted=was_aborted,
                        had_error=had_error,
                    )

                app.schedule_gui(_fin_simple)

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
        if self.valve_panel is not None:
            self.valve_panel.update_port_choices(ports)
        if ports:
            messagebox.showinfo("COM Ports", "Detected: " + ", ".join(ports), parent=self)
        else:
            messagebox.showinfo("COM Ports", "No COM ports detected. Manual entry is still available.", parent=self)

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
            if self.valve_panel is not None:
                vd = self.valve_panel.driver
                if vd is not None and vd.is_open:
                    try:
                        with self.valve_panel._serial_lock:
                            vd.force_stop()
                    except Exception:
                        pass

        threading.Thread(target=work, daemon=True).start()

    def switch_valve_to_pump(self, pump_index: int) -> None:
        """Move the selector valve to whichever port the active recipe (or last applied)
        has mapped to *pump_index*. Shows a popup if no mapping is available."""
        vp = self.valve_panel
        if vp is None:
            return
        port = self._active_pump_port_map.get(pump_index)
        if port is None:
            messagebox.showinfo(
                "Switch valve",
                f"No valve port is mapped to Pump {pump_index} yet.\n\n"
                f"Open the Selector Valve tab and fill in the "
                f"'Pump → Valve port' table to tell the app which line "
                f"each pump is plumbed to. (Recipe-specific overrides can "
                f"still be set per-recipe in the sequence editor.)",
                parent=self,
            )
            return
        if not vp.is_connected:
            messagebox.showinfo(
                "Switch valve",
                "The selector valve is not connected. Open the Selector Valve tab and "
                "click Connect Valve, then try again.",
                parent=self,
            )
            return
        vp.move_to_port_async(port)

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
        if self._pumps_canvas is not None:
            try:
                self._pumps_canvas.configure(bg=theme["bg"])
            except tk.TclError:
                pass
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
        if self._overview_tab is not None:
            try:
                self._overview_tab.refresh()
            except tk.TclError:
                pass
        if self.valve_panel is not None:
            try:
                self.valve_panel.refresh_mapping_nicknames()
                self.valve_panel.refresh_port_button_text()
            except tk.TclError:
                pass
        self.after(1000, self._schedule_live_updates)

    def on_close(self) -> None:
        self._recipe_abort_event.set()
        for panel in self.panels:
            panel.connection.close()
        if self.vacuum_panel is not None:
            self.vacuum_panel.close()
        if self.valve_panel is not None:
            try:
                self.valve_panel.close()
            except Exception:
                pass
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
