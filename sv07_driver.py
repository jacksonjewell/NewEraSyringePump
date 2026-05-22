"""
RUNZE SV-07 multiport selector valve driver.

Implements the official Runze "Send Command" 8-byte frame protocol over RS-232/RS-485:

    | B0 STX | B1 ADDR | B2 FUNC | B3 P1 | B4 P2 | B5 ETX | B6 SUM_LO | B7 SUM_HI |

  STX = 0xCC,  ETX = 0xDD.  Sum check is the little-endian 16-bit sum of bytes 0..5.

Function codes used here (per the SV-04/SV-07 manual):
    0x44  Move to port (motor selects shortest path)            param B3 = port (1..N)
    0x45  Reset to home (between port 1 and max)                param 0x00 0x00
    0x49  Force stop                                            param 0x00 0x00
    0x4A  Query motor status                                    param 0x00 0x00
    0x3E  Query current channel position (some firmwares)
    0x3F  Query current channel position (alternate)

Some Runze SV firmwares reply to position query with 0x3E vs 0x3F; this driver tries
0x3E first and falls back to 0x3F automatically the first time it gets a checksum/STX
mismatch on the position query, then sticks with whichever works.

Status response convention used by this driver (per common Runze SV-07 firmware):
    B2 == 0x00 -> idle / OK (motor stopped)
    B2 == 0x04 -> motor busy (still moving)
    other       -> error code

This module is GUI-agnostic; safe to import from a Tk worker thread. It does *not*
spin its own threads -- the caller is responsible for running blocking I/O off the
main thread.
"""

from __future__ import annotations

import time
from typing import Optional

import serial


STX = 0xCC
ETX = 0xDD

FUNC_MOVE_AUTO = 0x44
FUNC_RESET = 0x45
FUNC_FORCE_STOP = 0x49
FUNC_QUERY_STATUS = 0x4A
FUNC_QUERY_POS_PRIMARY = 0x3E
FUNC_QUERY_POS_FALLBACK = 0x3F

DEFAULT_BAUD = 9600
DEFAULT_TIMEOUT = 1.0

# Status byte (B2) interpretation in motor-status replies (0x4A).
STATUS_IDLE = 0x00
STATUS_BUSY = 0x04


class SV07Error(RuntimeError):
    """Raised when the valve returns a malformed reply or a documented error code."""


def _checksum(frame6: bytes) -> bytes:
    total = sum(frame6) & 0xFFFF
    return bytes([total & 0xFF, (total >> 8) & 0xFF])


def build_frame(addr: int, func: int, b3: int = 0x00, b4: int = 0x00) -> bytes:
    frame6 = bytes([STX, addr & 0xFF, func & 0xFF, b3 & 0xFF, b4 & 0xFF, ETX])
    return frame6 + _checksum(frame6)


def verify_response(resp: bytes) -> tuple[bool, str]:
    if len(resp) != 8:
        return False, f"incomplete response ({len(resp)} bytes)"
    if resp[0] != STX:
        return False, f"bad STX 0x{resp[0]:02X}"
    if resp[5] != ETX:
        return False, f"bad ETX 0x{resp[5]:02X}"
    expected = _checksum(resp[:6])
    if resp[6:] != expected:
        return False, (
            f"checksum mismatch got {resp[6]:02X} {resp[7]:02X}, "
            f"expected {expected[0]:02X} {expected[1]:02X}"
        )
    return True, "ok"


def status_text(byte_b2: int) -> tuple[str, bool]:
    """Return (human text, busy?) for a motor-status (0x4A) reply byte."""
    if byte_b2 == STATUS_IDLE:
        return "Idle", False
    if byte_b2 == STATUS_BUSY:
        return "Moving", True
    return f"Error 0x{byte_b2:02X}", False


class SV07:
    """
    Thin, blocking serial wrapper around the SV-07 protocol. Open the port, then call
    :meth:`move`, :meth:`get_position`, :meth:`get_status`, or :meth:`move_and_wait`.

    All methods that touch the serial port may raise ``SV07Error`` (bad frame /
    documented error reply) or the underlying ``serial.SerialException``.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUD,
        address: int = 0,
        timeout: float = DEFAULT_TIMEOUT,
        max_ports: int = 6,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.address = address
        self.timeout = timeout
        self.max_ports = max_ports
        self.ser: Optional[serial.Serial] = None
        # Position-query opcode: starts on PRIMARY, may flip to FALLBACK after one bad reply.
        self._pos_func = FUNC_QUERY_POS_PRIMARY
        self._pos_func_locked = False

    @property
    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def open(self) -> None:
        if self.is_open:
            return
        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self.timeout,
        )

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def _exchange(self, packet: bytes, response_len: int = 8, settle_s: float = 0.06) -> bytes:
        if not self.is_open:
            raise SV07Error("serial port is not open")
        assert self.ser is not None
        self.ser.reset_input_buffer()
        self.ser.write(packet)
        self.ser.flush()
        if settle_s > 0:
            time.sleep(settle_s)
        resp = self.ser.read(response_len)
        ok, msg = verify_response(resp)
        if not ok:
            raise SV07Error(f"bad response: {msg}; raw={resp.hex(' ')}")
        return resp

    def move(self, port: int) -> None:
        """Send a move-to-port command. Does not wait for completion."""
        if port < 1 or port > self.max_ports:
            raise ValueError(f"port {port} out of range 1..{self.max_ports}")
        self._exchange(build_frame(self.address, FUNC_MOVE_AUTO, port, 0x00))

    def reset_home(self) -> None:
        self._exchange(build_frame(self.address, FUNC_RESET))

    def force_stop(self) -> None:
        self._exchange(build_frame(self.address, FUNC_FORCE_STOP))

    def get_status(self) -> tuple[str, bool]:
        """Return (text, busy). ``busy=True`` means the motor is still moving."""
        resp = self._exchange(build_frame(self.address, FUNC_QUERY_STATUS))
        return status_text(resp[2])

    def get_position(self) -> int:
        """Return the current channel position (1..max_ports)."""
        try:
            resp = self._exchange(build_frame(self.address, self._pos_func))
        except SV07Error:
            if self._pos_func_locked:
                raise
            # Try the other opcode once. Some firmwares use 0x3F instead of 0x3E.
            self._pos_func = (
                FUNC_QUERY_POS_FALLBACK
                if self._pos_func == FUNC_QUERY_POS_PRIMARY
                else FUNC_QUERY_POS_PRIMARY
            )
            resp = self._exchange(build_frame(self.address, self._pos_func))
            self._pos_func_locked = True
        else:
            self._pos_func_locked = True
        return resp[3]

    def move_and_wait(
        self,
        port: int,
        poll_interval_s: float = 0.15,
        timeout_s: float = 10.0,
        abort: Optional[object] = None,
    ) -> int:
        """
        Move to *port* and poll status until idle or *timeout_s*. If *abort* is supplied
        and is set (anything that has ``is_set()``), this returns -1 immediately without
        further polling. Returns the final reported position on success.

        Raises :class:`SV07Error` if the firmware reports an error status code (B2 is
        anything other than IDLE/BUSY) or if the rotor's reported position does not
        match the requested *port* after the motor reports idle. Treating either of
        those silently as success would let downstream recipe steps dispense fluid
        through the wrong valve port — a serious cross-contamination / safety risk.
        """
        self.move(port)
        deadline = time.monotonic() + timeout_s
        last_status = "unknown"
        while time.monotonic() < deadline:
            if abort is not None and getattr(abort, "is_set", lambda: False)():
                return -1
            resp = self._exchange(build_frame(self.address, FUNC_QUERY_STATUS))
            b2 = resp[2]
            text, busy = status_text(b2)
            last_status = text
            if busy:
                time.sleep(poll_interval_s)
                continue
            if b2 != STATUS_IDLE:
                # Any non-idle, non-busy status byte is a documented firmware error
                # (motor fault, mechanical jam, encoder mismatch, …). The rotor has
                # NOT reached the requested port, so we must not let the caller
                # treat the reported position as a successful move.
                raise SV07Error(
                    f"valve reported error status 0x{b2:02X} ({text}) after move "
                    f"to port {port}; aborting to avoid wrong-port dispense"
                )
            final = self.get_position()
            if final != port:
                # Firmware reports idle but the rotor is at a different port from
                # the one we requested (e.g. motor stall before completion).
                # Refuse to claim success; recipe runner will surface this as an
                # error and stop before any pump step uses the wrong line.
                raise SV07Error(
                    f"valve idle at port {final} after requesting port {port}; "
                    f"move did not reach the target — aborting to avoid wrong-port dispense"
                )
            return final
        raise SV07Error(
            f"valve move did not complete within {timeout_s:.1f}s; last status: {last_status}"
        )


__all__ = [
    "SV07",
    "SV07Error",
    "STX",
    "ETX",
    "STATUS_IDLE",
    "STATUS_BUSY",
    "DEFAULT_BAUD",
    "DEFAULT_TIMEOUT",
    "build_frame",
    "verify_response",
    "status_text",
]
