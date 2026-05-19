# NE-1000 Syringe Pump Controller + Arduino Vacuum + RUNZE SV-07

A Python/Tkinter GUI for controlling **up to ten New Era NE-1000 syringe pumps**, an
**Arduino-driven vacuum pump + MPX5100DP pressure sensor**, and a **RUNZE SV-07
multiport selector valve**, all from a single tabbed interface.

Built for the Wanunu Lab at Northeastern University.

## Features

### Layout — tabbed home page

The main window is now organized as a `ttk.Notebook` with four tabs. The default
landing page is **Overview**.

| Tab | Contents |
|-----|----------|
| **Overview** | Compact summary cards: per-pump 1-line status with **Run / Stop / → Valve** buttons, live vacuum readout (bar/kPa/inHg) with **Vacuum ON/OFF** + **Connect**, and a current valve port + 1–N port buttons + **Connect Valve**. |
| **Pumps** | A **Custom inlets** quick-access bar at the top (one button per labeled non-pump port — e.g. *Vent*, *Waste* — that moves the valve in one click) followed by full pump panels (one per configured pump) in a vertically scrollable grid. |
| **Vacuum / Pressure** | The full Arduino vacuum + MPX5100DP pressure panel. |
| **Selector Valve** | The RUNZE SV-07 panel: COM/baud/address/max-ports, Connect/Disconnect, Refresh status, Reset to home, current port, port-button grid (each button shows what's on the line), a manual move-and-wait, and side-by-side **Pump → Valve port** + **Custom port labels** tables. |

The toolbar above the tabs (mode, **Pumps:** spinner, refresh ports, dark mode,
recipe drop-down, **Run recipe**, **Abort recipe**, **Recipes…**, **Power Off (Stop
All)**) stays visible on every tab.

### Pump support — 1 to 10 pumps

- A **Pumps:** spinner in the toolbar (range **1–10**, default **3**) lets you grow
  or shrink the panel grid live. Adding pumps creates fresh panels; removing
  pumps disconnects and destroys the trailing ones.
- Pump panels lay out in a **2-column scrollable grid** in the Pumps tab.
- The pump count is persisted in `pump_labels.json` (`num_pumps`) so the layout
  survives restarts. Loading a recipe also resizes the grid to match its
  `num_pumps` field.

### Per-pump panel

- **BD syringe presets** (1 mL through 60 mL) plus custom diameter
- **Rate units**: mL/min, mL/hr, uL/min, uL/hr
- **Dispense modes**: Continuous or Volume. In **Volume** mode a volume entry plus
  a **unit selector** (uL / mL / L) appear next to the Dispense Mode dropdown.
- **Direction**: Infuse / Withdraw
- **Live status**: volume dispensed, total volume, elapsed time (~1 s polling), on its own row under the panel title so it stays visible
- **Volume display units**: per-pump **Display as** dropdown (uL, mL, L)
- **Action buttons**: **Pump Auto-Connect** + **Apply pump settings** (yellow) on
  one row; **Run** (green) and **Stop** (red) on the next row; a full-width
  **→ Switch valve to this line** button on the third row that uses the active
  recipe's pump-to-port mapping.
- **Mode selector**: Individual / Dual / Triple mode with **Switch Together**
- **Power Off (Stop All)**: red/yellow emergency stop for all pumps + vacuum +
  valve (sends valve `force_stop`)

### Vacuum + pressure (Arduino)

- Toggle vacuum ON/OFF via serial (sends `1` / `0`); the toggle button is **orange
  when OFF** and **blinking blue when ON**
- **Live vacuum readout (MPX5100DP)** at ~10 Hz, shown in three units while
  connected:
  - **bar** (signed: `+0.00 bar` at rest, `-0.80 bar` under vacuum) — largest
    bold label
  - **kPa** — magnitude of vacuum below atmosphere (0–100 kPa)
  - **inHg** — same value converted to inches of mercury
- Readouts initialize to `0.00` the moment the Arduino connects, update
  continuously regardless of motor state, and reset to `---` only on disconnect.
  The reader runs in a background thread and recovers from disconnects /
  malformed lines.

### Selector valve (RUNZE SV-07)

- Full driver in **`sv07_driver.py`** implementing the official Runze 8-byte
  frame protocol (`STX 0xCC … ETX 0xDD` + 16-bit little-endian sum check).
- Function codes used: `0x44` move to port (auto shortest path), `0x45` reset
  home, `0x49` force stop, `0x4A` query motor status, and `0x3E` / `0x3F`
  query current channel position. The driver tries `0x3E` first and
  auto-falls-back to `0x3F` on the first malformed reply, then locks in
  whichever opcode worked.
- Configurable **max ports** (6 / 8 / 10 / 12 / 16) and **address**.
- **Move + wait**: sends move, polls status until idle, returns the final
  position. Honors the recipe **Abort** event — a long valve move can be cut
  short.
- A **port-button grid** lights up green on the currently-occupied port.
  Each button is captioned with whatever is wired to that port — pump nickname
  (for pump-mapped ports) or custom label (for vents / bleeds / waste / etc.).

### Pump → Valve port mapping (live, in-tab)

The **Selector Valve** tab has a **Pump → Valve port** table that lets you set,
right there, which valve port each pump's tubing is plumbed to (the live
default used when no recipe has overridden `pump_port_map`):

- One row per pump, with a port spinner (1–16) and a **Clear** button.
- Saves automatically to `pump_labels.json` (`pump_port_map`); a green
  "Saved" indicator flashes on each edit. Survives restarts.
- The **→ Switch valve to this line** button on each pump panel uses this
  mapping. So does the `valve_to_pump` recipe step.
- Recipes can store their own `pump_port_map` that overrides the live
  mapping while the recipe is loaded; on recipe end, the saved global
  mapping is restored. Editing the table never overwrites a recipe.

### Custom port labels (vents, bleeds, waste, atmosphere)

Not every valve port is a pump. The **Custom port labels** table on the
Selector Valve tab lets you give a free-text name to any port — useful for
bleed lines, vents, waste, atmospheric inlet, manual reservoirs, etc.

- One row per port (sized by `max_ports`), with an **(Pump N)** hint next
  to ports already claimed by a pump.
- Saves automatically to `pump_labels.json` (`port_labels`).
- Labels appear on the **Move to port** buttons of the valve grid and on
  the **Custom inlets** bar at the top of the Pumps tab (one button per
  labeled port, one click moves the valve there).
- Use the **`valve_to_label`** recipe step to switch to a labeled
  port by name (e.g. *Vent to atmosphere*) — resolved to a port at run
  time, so you can rewire without rewriting recipes.

### Recipes

The Recipes window (toolbar **Recipes…**) saves the current state of **every
pump panel currently shown**, the valve connection snapshot (from the Selector
Valve tab when you save), labels, **per-recipe** `pump_port_map`, and optional
sequence steps. Stored in **`recipes.json`** next to the script.

Two ways to execute:

- **Apply + Run all** — no `steps`: loads settings and runs every **already
  connected** pump (good for simple “run my rates” setups).
- **Run sequence** — recipe has `steps`: runs them **in strict order**: connect,
  vacuum ON/OFF, valve connect, valve moves, apply/run/stop pumps, delays, etc.

#### Sequence editor (**Edit sequence…**)

The floating **Sequence — …** window is where ordered protocols are authored.

| Area | Contents |
|------|----------|
| **Add step** groups | **Pumps** · **Vacuum (Arduino)** · **Selector valve (SV-07)** · one **Disconnect everything** button for end-of-protocol cleanup |
| Placement | Controls are intentionally **above** the step **list**, so valve buttons (**Valve connect…**, **Valve → port…**, etc.) stay on screen regardless of window size |
| **Pump↔Port mapping…** | Opens the per-recipe pump→valve port table (below) |

**Selector valve in sequences:** add a **`valve_connect`** step (with the same
COM/baud/address/`max_ports` you use on the valve tab) **before** any
`valve_to_port` / `valve_to_pump` / `valve_to_label` step. Use **Valve → port…**
for a numbered port (the dialog lists **custom labels** from the live Selector
Valve tab and respects **max ports**), **Valve → line for pump…** for the
per-recipe map, or **Valve → custom inlet…** to match a **Custom port label**
by name.

**Timed pumping in Continuous mode:** `run_pump` returns immediately in
Continuous mode — add a **`delay`** for the run duration, then **`stop_pump`**
(or use **Volume** mode so `run_pump` blocks until the volume is done).

#### Per-recipe pump-to-port mapping

Each recipe can store its own **`pump_port_map`** (e.g. Pump 2 → valve port 5).
That map is used by **Valve → line for pump N** recipe steps and by the main
window’s **→ Switch valve to this line** buttons while the recipe’s mapping is
active during a sequence run.

The **Pump↔Port mapping…** dialog:

- Has **one editable row per pump** in your current layout (not one row per
  physical rotor port — vents, bleeds, sealed caps, etc. are not “pumps”).
- Shows a **read-only reference list of all ports 1…N** (N = max ports from the
  live Selector Valve panel) with **Custom port labels** when set, so you can see
  the full rotor at a glance while assigning pump lines.
- Validates port numbers against that same **N** (not a flat 1–16 when the valve
  is configured for fewer ports).

Non-pump lines are still switched with **Valve → port…** or **Valve → custom
inlet…**; configure names on **Selector Valve → Custom port labels**.

#### Step types (sequences)

In addition to pump / vacuum / delay / `line_check` steps, sequences can
include:

| `type` | Description |
|--------|-------------|
| `valve_connect` | Open the SV-07 serial port (`com`, `baud`, `address`, `max_ports`) |
| `valve_disconnect` | Close the SV-07 serial port |
| `valve_to_port` | Move the valve to port `port` (1…N for your configured `max_ports`) and wait until idle |
| `valve_to_pump` | Move the valve to the port listed in the recipe’s `pump_port_map` for that `pump` index |
| `valve_to_label` | Move to the port whose **Custom port label** matches `label` (case-insensitive, trimmed; from live `port_labels` while running) |
| `disconnect_everything` | Best-effort: stop connected pumps, vacuum OFF, close vacuum + valve serial, disconnect all pump panels |

The legacy **`line_check`** step still works (operator confirms a popup before
the next step).

#### Recipe runner behavior

- The pre-run confirmation dialog shows **PUMPS / VACUUM / VALVE / STEPS** summary
  cards for sequences.
- The runner resizes the pump grid to the recipe’s `num_pumps` before running
  when needed.
- Friendly error messages cover valve failures (port not open, checksum,
  missing `pump_port_map` for `valve_to_pump`, unknown `valve_to_label`, motor
  timeout, etc.).
- Estimated time budgets ~3 s per valve move for the progress bar.

### Other

- **Manual COM port entry**: develop and configure without hardware connected
- **Progress bar**: during recipe execution, bottom-right shows estimated %
  complete and time remaining; pauses cleanly during operator confirmations.
- **Dark / Light mode** toggle (the scrollable Pumps tab background follows the
  theme).

## Requirements

- Python 3.x
- Git for Windows (if cloning the repo)
- New Era NE-1000 Syringe Pumps (1–10)
- Arduino Uno + MPX5100DP pressure sensor + vacuum motor relay
- RUNZE SV-07 selector valve with USB-to-RS232/RS485 adapter
  (factory default 9600 baud, address 0)

## Setup

```powershell
git clone https://github.com/jacksonjewell/NewEraSyringePump.git
cd NewEraSyringePump
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

On macOS/Linux substitute `python3 -m venv .venv`.

### If `.venv` is broken (“No Python at …”, exit code 103)

```powershell
Remove-Item -Recurse -Force .venv
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

## Run

```powershell
.\.venv\Scripts\python pump_control_gui.py
```

### Local data files

| File | Purpose |
|------|---------|
| `recipes.json` | Saved recipes / sequences. **Committed** so you can pull the same recipes on another machine. Contains COM port names — edit after pull if hardware differs. |
| `pump_labels.json` | Pump nicknames, chosen pump count (`num_pumps`), live pump→valve-port mapping (`pump_port_map`), and custom port labels (`port_labels`). **Committed** for the same reason. |
| **`recipes.example.json`** | Reference template; the “with sequence” recipe includes sample **valve** steps (`valve_connect`, `valve_to_port`, `valve_disconnect`) after the vacuum block. |

After you change recipes or nicknames in the GUI, run **`.\push_recipes.ps1`**
(or `git add` / `commit` / `push` those two files yourself) so the other machine
gets the update. The app does not auto-push.

### `pump_labels.json` format

```json
{
  "num_pumps": 5,
  "pumps": {
    "1": "Buffer",
    "2": "Sample",
    "3": "Wash",
    "4": "Waste-pull",
    "5": "Hexadecane"
  },
  "pump_port_map": {
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5
  },
  "port_labels": {
    "6": "Vent to atmosphere"
  }
}
```

| Field | Meaning |
|-------|---------|
| `num_pumps` | Pump count the panel grid was last saved with (1–10). |
| `pumps` | Pump nicknames keyed by 1-based pump index. |
| `pump_port_map` | Optional default **pump → valve port** mapping used by the **→ Switch valve** buttons and `valve_to_pump` steps. Edit live in the **Pump → Valve port** table on the Selector Valve tab. Recipes can override this per-recipe without overwriting the global default. |
| `port_labels` | Optional **port → free-text label** for non-pump inlets (vents, bleeds, waste, atmosphere, manual reservoirs). Drives the labels on the Move-to-port buttons, the Custom inlets bar, and the `valve_to_label` recipe step. Edit live in the **Custom port labels** table on the Selector Valve tab. |

The legacy flat shape (`{"1": "Buffer", "2": "Sample", "3": "Wash"}` with no
`num_pumps` field) is still readable; on the next save the GUI rewrites it in
the new shape.

### `recipes.json` format

Top-level shape:

```json
{ "version": 1, "recipes": [ /* array of recipe objects */ ] }
```

Each **recipe** object (new schema, written by the GUI):

| Field | Description |
|-------|-------------|
| `id` | UUID. Required to round-trip step edits to the same recipe. |
| `name` | Display name. |
| `num_pumps` | Pump count this recipe was saved with (1–10). |
| `pumps` | `{ "1": {...settings}, "2": {...settings}, ... }` |
| `pump_conns` | `{ "1": {com, baud, address}, ... }` |
| `pump_labels` | `{ "1": "Buffer", "2": "Sample", ... }` |
| `valve_conn` | `{ com, baud, address, max_ports }` (optional) |
| `pump_port_map` | `{ "1": 1, "2": 3, ... }` — valve port for each pump (optional) |
| `steps` | Optional ordered sequence (see below). |

**Backward compatibility:** the loader still accepts the old flat shape with
`pump1`, `pump2`, `pump3` and `pump1_conn`, `pump2_conn`, `pump3_conn` keys.
Saves always use the new schema.

**Pump settings** (entries in `pumps`): `syringe`, `custom_diameter_mm`,
`rate_units`, `rate_value`, `dispense_mode`, `volume_ul`, `direction`.

**Sequence `steps`** — each is an object with `type` and type-specific fields.
Optional `label` annotates the step in the editor list.

| `type` | Extra fields |
|--------|--------------|
| `delay` | `seconds` |
| `connect_pump` | `pump`, `com`, `baud`, `address` |
| `disconnect_pump` | `pump` |
| `apply_pump` | `pump`, `settings` |
| `run_pump` | `pump` |
| `stop_pump` | `pump` |
| `line_check` | `pump` (legacy: operator confirms popup) |
| `vacuum_connect` | `com`, `baud` |
| `vacuum_disconnect` | *(none)* |
| `vacuum_on` | *(none)* |
| `vacuum_off` | *(none)* |
| `valve_connect` | `com`, `baud`, `address`, `max_ports` |
| `valve_disconnect` | *(none)* |
| `valve_to_port` | `port` (1…N for the valve’s configured `max_ports`) |
| `valve_to_pump` | `pump` — resolved via `pump_port_map` at runtime |
| `valve_to_label` | `label` — resolved via `port_labels` at runtime (case-insensitive, trimmed) |
| `disconnect_everything` | *(none)* — tear down pumps, vacuum, and valve serial |

## Arduino Vacuum Sketch

The sketch is in the repo at **`arduino/VacuumPumpV1/VacuumPumpV1.ino`**. Open
that folder in Arduino IDE and upload to an Uno.

**Wiring (Arduino Uno):**

- Vacuum motor relay/transistor signal → **D9**
- Indicator LED (in series with ~220 Ω resistor) → **D3**, cathode → GND
- **MPX5100DP** pressure sensor signal → **A0**, sensor `Vs` → 5 V, `GND` → GND
- Sensor port: connect one port to the vacuum line; leave the other open to
  atmosphere (so it reads the differential)

**Serial protocol (9600 baud):**

Commands the GUI sends to the Arduino:

- `1` — motor + LED ON, Arduino replies `MOTOR:ON`
- `0` — motor + LED OFF, Arduino replies `MOTOR:OFF`

Telemetry the Arduino streams continuously (~10 Hz, always while connected):

```
VACUUM_KPA:<kpa>,INHG:<inhg>
```

The GUI's vacuum panel listens for these lines and updates the
**bar / kPa / inHg** readouts live. The conversion to bar (for the signed
"vacuum gauge" display) is `bar = -kPa / 100`, so 80 kPa of vacuum reads as
`-0.80 bar`.

## RUNZE SV-07 Selector Valve

**Driver module:** `sv07_driver.py` — pure Python, GUI-agnostic, all blocking
I/O is the caller's responsibility (the GUI runs valve commands on background
threads).

**Wiring (USB → RS-232 / RS-485 adapter):**

- USB adapter → SV-07 communications port
- 24 V DC supply → SV-07 power port (the valve will not move without it)
- Adapter's COM port → set in the **Selector Valve** tab (default `COM4`)

**Default settings:** baud 9600, address 0, 6 ports. Change via the panel;
both are persisted per-recipe in `valve_conn`.

**Protocol summary** (8-byte frames):

```
| B0 STX | B1 ADDR | B2 FUNC | B3 P1 | B4 P2 | B5 ETX | B6 SUM_LO | B7 SUM_HI |

  STX = 0xCC,  ETX = 0xDD
  SUM = (B0 + B1 + B2 + B3 + B4 + B5)  &  0xFFFF, little-endian

Function codes used:
  0x44  Move to port (auto shortest path)        param B3 = port (1..N)
  0x45  Reset to home (between port 1 and N)
  0x49  Force stop
  0x4A  Query motor status                       reply B2: 0x00 idle, 0x04 busy
  0x3E  Query current channel position           reply B3 = port number
  0x3F  Same query (alternate firmware opcode; auto fallback if 0x3E fails once)
```

## Project Structure

```
pump_control_gui.py              Main GUI application
sv07_driver.py                   RUNZE SV-07 protocol driver
pump_environment_check.py        Quick hardware/environment readiness check
arduino/VacuumPumpV1/            Arduino vacuum sketch (VacuumPumpV1.ino)
README.md                        This file — setup, recipe steps, hardware notes
recipes.example.json             Reference example (legacy + sequence steps incl. valve)
recipes.json                     Saved recipes
pump_labels.json                 Pump nicknames + saved pump count
push_recipes.ps1                 Commit + push recipes.json and pump_labels.json only
requirements.txt                 Python dependencies
.gitignore                       Excludes venv, cache, IDE files, logs
```

## Dependencies

- [NESP-Lib](https://github.com/florian-lapp/nesp-lib-py) — New Era Syringe Pump library for Python
- [pyserial](https://pypi.org/project/pyserial/) — used by both the Arduino vacuum panel and the SV-07 driver
