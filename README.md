# NE-1000 Syringe Pump Controller + Arduino Vacuum Control

A Python/Tkinter GUI for controlling up to three **New Era NE-1000** syringe pumps and an **Arduino-driven vacuum pump** from a single interface.

Built for the Wanunu Lab at Northeastern University.

## Features

- **3 independent pump panels** with per-pump COM port and address configuration
- **BD syringe presets** (1 mL through 60 mL) with custom diameter support
- **Rate units**: mL/min, mL/hr, uL/min, uL/hr
- **Dispense modes**: Continuous or Volume-based (volume to dispense is shown only in **Volume** mode on each pump panel and in **Apply pump** sequence steps)
- **Direction**: Infuse / Withdraw
- **Live status**: volume dispensed, total volume, elapsed time
- **Mode selector**: Individual, Dual, or Triple mode with "Switch Together" toggle
- **Power Off (Stop All)**: emergency stop for all pumps + vacuum
- **Dark / Light mode** toggle
- **Arduino vacuum panel**: connect to Arduino Uno, toggle vacuum ON/OFF via serial (sends `1`/`0`), displays Arduino reply (`Motor ON`/`Motor OFF`)
- **Manual COM port entry**: develop and configure without hardware connected
- **Main toolbar recipes**: **Recipe** drop-down lists all saved recipes; **Run recipe** runs the selected one (sequence if it has steps, otherwise **Apply + Run all**). While running, **Run recipe** blinks red; **Abort recipe** stops between steps and during delays (a step already in progress may finish first).
- **Recipes** (floating window via **Recipes…**): save syringe/rate/volume/direction **and** COM/baud/address for pumps 1–3; **Apply to pump panels** loads those fields. **Edit sequence…** opens a larger **Sequence** window to add ordered steps: delays, pump connect/disconnect/apply/run/stop, vacuum connect/disconnect/on/off. Reorder steps by **dragging** a row in the list or with **Move up / down**. **Edit step…** or **double-click** a row to change that step (same dialogs as when adding; vacuum ON/OFF/disconnect have no extra fields). Optional **Step label…** / **Clear step label** annotate each step in the list. **Run sequence** executes steps in order. Simple recipes without steps still use **Apply + Run all**. Data is stored in `recipes.json` next to the script.
- **Pump display names**: each pump panel has a **Display name** field; the group title becomes `Pump N — Your name`. Names are saved to `pump_labels.json` and are also stored on **Save from main window** (as `pump_labels` in the recipe) and restored with **Apply to pump panels**.

## Requirements

- Python 3.x
- Git for Windows (if using Claude Code or cloning repo)

## Setup

```powershell
git clone https://github.com/jacksonjewell/NewEraSyringePump.git
cd NewEraSyringePump
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```

## Run

```powershell
.\.venv\Scripts\python pump_control_gui.py
```

If Windows blocks the script, call the interpreter explicitly (as above) or run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

### Local data files

| File | Purpose |
|------|---------|
| `recipes.json` | Saved recipes and sequences (created by the app; **gitignored** — contains COM ports and lab-specific settings). |
| `pump_labels.json` | Pump display names (auto-saved; **gitignored**). |

To start from an empty recipe list in a new clone, copy `recipes.example.json` to `recipes.json` or save a recipe once from **Recipes…**.

## Arduino Vacuum Sketch

Flash `VacuumPumpV1.ino` to an Arduino Uno. The sketch listens at 9600 baud and accepts:

- `1` — motor + LED ON
- `0` — motor + LED OFF

The GUI's Vacuum Control panel connects to the Arduino's COM port and sends these commands via the toggle button.

## Project Structure

```
pump_control_gui.py         Main GUI application
pump_environment_check.py   Quick hardware/environment readiness check
recipes.example.json        Example empty recipe store (copy to recipes.json locally)
recipes.json                Your saved recipes (created by the app; listed in .gitignore)
pump_labels.json            Your pump nicknames (auto-saved; listed in .gitignore)
requirements.txt            Python dependencies (NESP-Lib, pyserial)
.gitignore                  Excludes venv, cache, IDE files, and local JSON data
```

## Dependencies

- [NESP-Lib](https://github.com/florian-lapp/nesp-lib-py) — New Era Syringe Pump Library for Python
- [pyserial](https://pypi.org/project/pyserial/) — Serial port access for Arduino communication

## License

Internal lab use.
