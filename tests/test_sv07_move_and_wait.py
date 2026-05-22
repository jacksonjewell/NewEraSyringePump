"""Regression tests for ``SV07.move_and_wait`` wrong-port / error-status bugs.

Two ways the previous implementation could silently lie to the caller about a
move succeeding, both with safety-critical downstream impact (next recipe step
dispenses fluid through the wrong valve port):

1. **Error status byte treated as idle.** ``status_text(b2)`` returns
   ``busy=False`` for *any* status byte that isn't ``0x04`` (BUSY) — including
   documented firmware error codes. The original ``move_and_wait`` only
   inspected ``busy``, so a motor-fault status (e.g. 0x03 jam, 0xFF bus error)
   exited the poll loop and queried position; the rotor had **not** moved, but
   ``get_position()`` returned whatever port it was previously on, which was
   then handed back to the recipe runner as if the move succeeded.

2. **Idle but not at the requested port.** Even on a clean ``STATUS_IDLE``,
   firmware can report idle while the rotor is mid-stall at the wrong port
   (loose belt, encoder mismatch, etc.). The driver returned the rotor's
   reported position regardless of whether it matched the requested port.

Concrete trigger for both: valve currently at port 5 (water flush). Recipe
step ``valve_to_pump 1`` requests port 3 (hazardous solvent). The motor
fails to actually move there. The pre-fix driver returns 5 to
``move_to_port_sync``, which reports "Idle (port 5)" and returns 5 — a
non-negative value, so the recipe runner happily proceeds to the next
``run_pump`` step and dispenses the syringe contents through the **water**
line. With dangerous reagents this is a safety incident.

These tests use a fake serial layer so they run without hardware on CI.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_serial_stub() -> None:
    if "serial" in sys.modules:
        return
    import types

    class _SerialException(Exception):
        pass

    class _Serial:
        def __init__(self, *a, **kw):
            self.is_open = True
        def close(self):
            self.is_open = False
        def reset_input_buffer(self):
            pass
        def write(self, data):
            return len(data)
        def flush(self):
            pass
        def read(self, n):
            return b""

    serial_mod = types.ModuleType("serial")
    serial_mod.Serial = _Serial
    serial_mod.SerialException = _SerialException
    sys.modules["serial"] = serial_mod
    tools_mod = types.ModuleType("serial.tools")
    list_ports_mod = types.ModuleType("serial.tools.list_ports")
    list_ports_mod.comports = lambda: []
    tools_mod.list_ports = list_ports_mod
    serial_mod.tools = tools_mod
    sys.modules["serial.tools"] = tools_mod
    sys.modules["serial.tools.list_ports"] = list_ports_mod


_install_serial_stub()

import sv07_driver  # noqa: E402
from sv07_driver import (  # noqa: E402
    ETX,
    STATUS_BUSY,
    STATUS_IDLE,
    STX,
    SV07,
    SV07Error,
    _checksum,
    build_frame,
)


def _make_response(addr: int = 0, b2: int = 0, b3: int = 0, b4: int = 0) -> bytes:
    """Build a well-formed 8-byte SV-07 response.

    On the SV-07 wire, response byte 2 carries the status code (the driver
    reads ``resp[2]`` for status and ``resp[3]`` for position) — for query
    replies, it's the documented status byte; for command acknowledgements
    a value of 0x00 is fine.
    """
    frame6 = bytes([STX, addr & 0xFF, b2 & 0xFF, b3 & 0xFF, b4 & 0xFF, ETX])
    return frame6 + _checksum(frame6)


class _ScriptedSerial:
    """Tiny serial fake whose ``read()`` returns the next scripted reply.

    For each ``write()`` from the driver, the next ``read(8)`` returns the
    head of ``replies``. Any extra reads return empty (caller-side timeout).
    """

    def __init__(self, replies: list[bytes]):
        self._replies = list(replies)
        self.is_open = True
        self.writes: list[bytes] = []

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, n: int) -> bytes:
        if not self._replies:
            return b""
        return self._replies.pop(0)

    def close(self) -> None:
        self.is_open = False


def _make_driver(replies: list[bytes]) -> SV07:
    d = SV07(port="FAKE", address=0, timeout=0.5, max_ports=6)
    d.ser = _ScriptedSerial(replies)
    # Mark position-query opcode locked so the auto-fallback path doesn't
    # interfere with reply scripting.
    d._pos_func_locked = True
    return d


class MoveAndWaitWrongPortTests(unittest.TestCase):
    """``move_and_wait`` must NOT report success when the rotor is at the wrong port."""

    def test_error_status_byte_raises_instead_of_returning_stale_position(self) -> None:
        # Sequence on the wire after move(port=3):
        #   write: move-to-port 3            -> reply: ack (any well-formed frame)
        #   write: query status (0x4A)       -> reply b2=0x03 (documented error)
        #
        # Pre-fix bug: status_text(0x03) -> ('Error 0x03', False), busy=False, so
        # move_and_wait would call get_position() and happily return whatever the
        # rotor reports. With this test we don't even script the position-query
        # reply: the fix raises *before* asking for position.
        # Script a position reply that says the rotor is at port 5 — the
        # *previous* port, since the motor faulted and never moved. Pre-fix
        # code would consume this reply and silently return 5 (a non-negative
        # value) to the caller, which the GUI/recipe layer then treats as a
        # successful move to port 3.
        replies = [
            _make_response(b2=0x00),                # ack for move
            _make_response(b2=0x03),                # status: error code
            _make_response(b2=0x00, b3=0x05),       # (pre-fix) position query
        ]
        d = _make_driver(replies)

        with self.assertRaises(SV07Error) as ctx:
            d.move_and_wait(3, poll_interval_s=0.0, timeout_s=2.0)

        msg = str(ctx.exception).lower()
        self.assertIn("error status", msg)
        self.assertIn("0x03", str(ctx.exception))

    def test_idle_at_wrong_port_raises_instead_of_returning_silently(self) -> None:
        # Motor reports idle, but at port 5 instead of the requested port 3.
        # Pre-fix code returned 5 to the caller (>= 0), which the GUI/recipe
        # layer treated as a successful move.
        replies = [
            _make_response(b2=0x00),                        # ack for move
            _make_response(b2=STATUS_IDLE),                 # status: idle
            _make_response(b2=0x00, b3=0x05),               # position: rotor at 5
        ]
        d = _make_driver(replies)

        with self.assertRaises(SV07Error) as ctx:
            d.move_and_wait(3, poll_interval_s=0.0, timeout_s=2.0)

        msg = str(ctx.exception)
        self.assertIn("port 5", msg)
        self.assertIn("port 3", msg)

    def test_idle_at_correct_port_still_returns_normally(self) -> None:
        # Sanity: a correct move still works.
        replies = [
            _make_response(b2=0x00),           # ack for move
            _make_response(b2=STATUS_BUSY),    # busy poll
            _make_response(b2=STATUS_IDLE),    # idle poll
            _make_response(b2=0x00, b3=0x03),  # position: rotor at 3 (target)
        ]
        d = _make_driver(replies)
        self.assertEqual(d.move_and_wait(3, poll_interval_s=0.0, timeout_s=2.0), 3)

    def test_abort_event_short_circuits(self) -> None:
        # Abort path is unchanged; verify it still returns -1 cleanly.
        replies = [_make_response(b2=0x00)]
        d = _make_driver(replies)
        ev = threading.Event()
        ev.set()
        self.assertEqual(d.move_and_wait(2, poll_interval_s=0.0, timeout_s=2.0, abort=ev), -1)


if __name__ == "__main__":
    unittest.main()
