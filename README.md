# NE-1000 Syringe Pump Controller + Arduino Vacuum Control

A Python/Tkinter GUI for controlling up to three **New Era NE-1000** syringe pumps and an **Arduino-driven vacuum pump** from a single interface.

Built for the Wanunu Lab at Duquesne University.

## Features

- **3 independent pump panels** with per-pump COM port and address configuration
- **BD syringe presets** (1 mL through 60 mL) with custom diameter support
- **Rate units**: mL/min, mL/hr, uL/min, uL/hr
- **Dispense modes**: Continuous or Volume-based
- **Direction**: Infuse / Withdraw
- **Live status**: volume dispensed, total volume, elapsed time
- **Mode selector**: Individual, Dual, or Triple mode with "Switch Together" toggle
- **Power Off (Stop All)**: emergency stop for all pumps + vacuum
- **Dark / Light mode** toggle
- **Arduino vacuum panel**: connect to Arduino Uno, toggle vacuum ON/OFF via serial (sends `1`/`0`), displays Arduino reply (`Motor ON`/`Motor OFF`)
- **Manual COM port entry**: develop and configure without hardware connected

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

## Arduino Vacuum Sketch

Flash `VacuumPumpV1.ino` to an Arduino Uno. The sketch listens at 9600 baud and accepts:

- `1` — motor + LED ON
- `0` — motor + LED OFF

The GUI's Vacuum Control panel connects to the Arduino's COM port and sends these commands via the toggle button.

## Project Structure

```
pump_control_gui.py         Main GUI application
pump_environment_check.py   Quick hardware/environment readiness check
requirements.txt            Python dependencies (NESP-Lib, pyserial)
.gitignore                  Excludes .venv, __pycache__, IDE files
```

## Dependencies

- [NESP-Lib](https://github.com/florian-lapp/nesp-lib-py) — New Era Syringe Pump Library for Python
- [pyserial](https://pypi.org/project/pyserial/) — Serial port access for Arduino communication

## License

Internal lab use.
