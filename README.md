# NE-1000 Syringe Pump Controller + Arduino Vacuum Control

A Python/Tkinter GUI for controlling up to three **New Era NE-1000** syringe pumps and an **Arduino-driven vacuum pump** from a single interface.

Built for the Wanunu Lab at Northeastern University.

## Features

- **3 independent pump panels** with per-pump COM port and address configuration
- **BD syringe presets** (1 mL through 60 mL) with custom diameter support
- **Rate units**: mL/min, mL/hr, uL/min, uL/hr
- **Dispense modes**: Continuous or Volume-based. In **Volume** mode, the volume-to-dispense entry and a **unit selector** (uL, mL, L) appear to the right of the Dispense Mode dropdown.
- **Direction**: Infuse / Withdraw
- **Live status**: volume dispensed, total volume, elapsed time — updated automatically via ~1 s polling
- **Volume display units**: each pump panel has a **Display as** dropdown (uL, mL, L) for Volume Dispensed and Total Volume Dispensed readouts
- **Mode selector**: Individual, Dual, or Triple mode with "Switch Together" toggle
- **Power Off (Stop All)**: red/yellow emergency stop button for all pumps + vacuum
- **Dark / Light mode** toggle
- **Styled action buttons**: **Pump Auto-Connect** and **Apply Settings** (yellow) share a row; **Run** (green) and **Stop** (red) share a row below. All action buttons illuminate on hover.
- **Arduino vacuum panel**: connect to Arduino Uno, toggle vacuum ON/OFF via serial (sends `1`/`0`), displays Arduino reply. Toggle button is **orange when OFF** and **blinking blue when ON**.
- **Manual COM port entry**: develop and configure without hardware connected
- **Recipe confirmation**: before running, a summary dialog shows pumps used, vacuum requirement, step list, and estimated run time. A **preflight check** verifies all hardware is connected; if not, a detailed error explains what's missing with suggestions to fix it.
- **Progress bar**: during recipe execution, a progress bar in the bottom-right shows estimated completion percentage and time remaining.
- **Main toolbar recipes**: **Recipe** drop-down lists all saved recipes; **Run recipe** shows the confirmation dialog, then runs the selected recipe (sequence if it has steps, otherwise **Apply + Run all**). While running, **Run recipe** blinks red; **Abort recipe** stops between steps and during delays (a step already in progress may finish first). A completion popup appears only on successful finish — not on errors or aborts.
- **Recipes** (floating window via **Recipes…**): save syringe/rate/volume/direction **and** COM/baud/address for pumps 1–3; **Apply to pump panels** loads those fields. **Edit sequence…** opens a larger **Sequence** window to add ordered steps: delays, pump connect/disconnect/apply/run/stop, vacuum connect/disconnect/on/off. Reorder steps by **dragging** a row in the list or with **Move up / down**. **Edit step…** or **double-click** a row to change that step (same dialogs as when adding; vacuum ON/OFF/disconnect have no extra fields). Optional **Step label…** / **Clear step label** annotate each step in the list. **Run sequence** executes steps in order. Simple recipes without steps still use **Apply + Run all**. Data is stored in `recipes.json` next to the script.
- **Pump display names**: each pump panel has a **Display name** field; the group title becomes `Pump N — Your name`. Names are saved to `pump_labels.json` and are also stored on **Save from main window** (as `pump_labels` in the recipe) and restored with **Apply to pump panels**.

## Requirements

- Python 3.x
- Git for Windows (if using Claude Code or cloning repo)

## Setup

After cloning, create a virtual environment and install dependencies. On Windows, **`py -3`** is recommended so the venv uses whatever Python 3 you have installed (avoids a broken `.venv` if you uninstall or move an old Python).

```powershell
git clone https://github.com/jacksonjewell/NewEraSyringePump.git
cd NewEraSyringePump
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

On macOS/Linux you can use `python3 -m venv .venv` instead of `py -3 -m venv .venv`.

### If `.venv` is broken (“No Python at …”, exit code 103)

The folder `.venv` is **not** in git — it was created on your machine and points at a specific Python path. If that Python was removed or moved, delete and recreate:

```powershell
Remove-Item -Recurse -Force .venv
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Each new computer (or fresh clone) should run the same `py -3 -m venv` + `pip install` steps once.

## Run

```powershell
.\.venv\Scripts\python pump_control_gui.py
```

If Windows blocks the script, call the interpreter explicitly (as above) or run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

### Local data files

| File | Purpose |
|------|---------|
| `recipes.json` | Saved recipes and sequences. **Committed to this repo** so you can `git pull` the same recipes on another computer. Contains **COM port names** (e.g. `COM3`); laptop vs desktop often need different ports — edit after pull if hardware differs. |
| `pump_labels.json` | Pump display names (auto-saved). **Committed** with the repo for the same reason. |
| **`recipes.example.json`** | Reference / template for the JSON shape (see below). |

**After you change recipes or nicknames in the GUI**, run **`.\push_recipes.ps1`** (or `git add` / `commit` / `push` those two files yourself) so the other machine gets updates. The app does not auto-push to GitHub.

### `recipes.json` format (and `recipes.example.json`)

The GUI reads/writes a single JSON file with this top-level shape:

```json
{
  "version": 1,
  "recipes": [ /* array of recipe objects */ ]
}
```

Each **recipe** object usually includes:

| Field | Description |
|-------|-------------|
| `id` | Unique string (UUID). Required for **Edit sequence…** to save steps back to the same recipe. |
| `name` | Display name in the Recipes window and main toolbar drop-down. |
| `pump1`, `pump2`, `pump3` | Syringe/rate/dispense/direction snapshots (same keys as the pump panels). |
| `pump1_conn`, `pump2_conn`, `pump3_conn` | `com` (string), `baud` (int, typically **19200** for NE-1000), `address` (int). |
| `pump_labels` | Optional map `"1"` / `"2"` / `"3"` → display name strings. |
| `steps` | Optional list of sequence steps. If **missing or empty**, **Run recipe** / **Apply + Run all** only applies panel settings and runs connected pumps. If **non-empty**, **Run recipe** runs this sequence in order. |

**`pump1` / `pump2` / `pump3` fields** (strings unless noted):

- `syringe` — preset name from the GUI (e.g. `"BD 10 mL (10 cc)"`) or `"Custom"`.
- `custom_diameter_mm` — inner diameter in mm.
- `rate_units` — one of `mL/min`, `mL/hr`, `uL/min`, `uL/hr`.
- `rate_value` — numeric string.
- `dispense_mode` — `Continuous` or `Volume`.
- `volume_ul` — µL to dispense when mode is `Volume`.
- `direction` — `Infuse` or `Withdraw`.

**Sequence `steps`** — each element is an object with `type` and type-specific fields. Optional `label` (or legacy `step_label`) adds text in the sequence list.

| `type` | Extra fields |
|--------|----------------|
| `delay` | `seconds` (number) |
| `connect_pump` | `pump` (1–3), `com`, `baud`, `address` |
| `disconnect_pump` | `pump` (1–3) |
| `apply_pump` | `pump` (1–3), `settings` (same shape as `pump1` / `pump2` / `pump3` above) |
| `run_pump` | `pump` (1–3) |
| `stop_pump` | `pump` (1–3) |
| `vacuum_connect` | `com`, `baud` (Arduino; often **9600**) |
| `vacuum_disconnect` | *(none)* |
| `vacuum_on` | *(none)* — sends `1` |
| `vacuum_off` | *(none)* — sends `0` |

The committed file **`recipes.example.json`** contains two recipes: one **without** `steps` (toolbar **Run recipe** behaves like apply + run), and one **with** a sample `steps` array you can trim or copy from.

## Arduino Vacuum Sketch

The sketch is in the repo at **`arduino/VacuumPumpV1/VacuumPumpV1.ino`** (Arduino IDE expects the folder name to match the `.ino` filename). Open that folder in Arduino IDE and upload to an Uno.

The sketch listens at **9600** baud and accepts:

- `1` — motor + LED ON
- `0` — motor + LED OFF

The GUI's Vacuum Control panel connects to the Arduino's COM port and sends these commands via the toggle button.

## Project Structure

```
pump_control_gui.py              Main GUI application
pump_environment_check.py        Quick hardware/environment readiness check
arduino/VacuumPumpV1/            Arduino vacuum sketch (VacuumPumpV1.ino)
recipes.example.json             Reference example of the JSON format
recipes.json                     Saved recipes (tracked in git for multi-PC sync)
pump_labels.json                 Pump nicknames (tracked in git)
push_recipes.ps1                 Commit + push recipes.json and pump_labels.json only
requirements.txt                 Python dependencies (NESP-Lib, pyserial)
.gitignore                       Excludes venv, cache, IDE files, logs
```

## Dependencies

- [NESP-Lib](https://github.com/florian-lapp/nesp-lib-py) — New Era Syringe Pump Library for Python
- [pyserial](https://pypi.org/project/pyserial/) — Serial port access for Arduino communication

## License

Internal lab use.
