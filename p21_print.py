#!/usr/bin/env python3

"""Print labels and read/change settings on a Nelko P21 (or P22) thermal label printer.

The printer speaks a small line-based command protocol over a Bluetooth Classic
serial connection (SPP / RFCOMM). Text commands and their replies end in CRLF;
two status commands are raw ESC byte sequences whose replies are raw bytes. See
README.md for the full protocol. This script implements the part needed to print
images and manage the printer.

Connect first, e.g.:
    rfcomm connect /dev/rfcomm0 XX:XX:XX:XX:XX:XX

Then, for example:
    p21_print.py --status                show the loaded label roll
    p21_print.py --config                show DPI, versions, timer and beep
    p21_print.py --battery               show the battery level
    p21_print.py --image label.png       print an image on the loaded label
    p21_print.py --test-pattern          print a density test pattern
    p21_print.py --timeout 30            set auto power-off (0, 15, 30 or 60 min)
    p21_print.py --beep off              turn the beep off
    p21_print.py --selftest              print the built-in self-test page
"""

import argparse
import struct
import sys
import time
from dataclasses import dataclass
from enum import IntEnum

import serial
from PIL import Image, ImageEnhance, ImageOps

# --- Printer constants --------------------------------------------------------
BAUD = 115200
DEFAULT_DEVICE = "/dev/rfcomm0"
DEFAULT_DPI = 203          # the printer's real DPI is read from CONFIG?
HEAD_WIDTH_MM = 12         # the thermal head is 96 dots = 12 mm wide at 203 DPI;
                           # a wider bitmap is clipped by the head.

# Auto power-off: the TIMER command takes a raw 0..3 byte; CONFIG? reports the same.
TIMER_TEXT = {0: "Never", 1: "15 minutes", 2: "30 minutes", 3: "60 minutes"}
MINUTES_TO_TIMER = {0: 0, 15: 1, 30: 2, 60: 3}

DEBUG = False
_port = None               # cached serial connection (see open_port)


# --- Enumerations from the protocol ------------------------------------------
class Status(IntEnum):
    """Printer status byte (reply to `ESC ! ?`, and byte 0 of the label block)."""

    READY = 0x00
    LID_OPEN = 0x01
    FEEDING = 0x02
    PAPER_ERROR = 0x03
    NO_PAPER = 0x04
    PAUSED = 0x10
    BUSY = 0x20
    OUT_OF_PAPER = 0x24
    LOW_BATTERY = 0x30
    OVERHEATED = 0x80
    NO_POWER = 0x90

    def __str__(self):
        return self.name.replace("_", " ").title()


class Color(IntEnum):
    """Label colour (byte 4 of the label block)."""

    UNKNOWN = 0
    TRANSPARENT = 2
    WHITE = 3
    PINK = 4
    BLUE = 5
    YELLOW = 6

    def __str__(self):
        return self.name.title()


class Style(IntEnum):
    """Label paper type (byte 7 of the label block)."""

    CONTINUOUS = 0
    GAPPED = 1
    BLACKMARK = 2

    def __str__(self):
        return self.name.title()


def _safe_enum(enum_cls, value, default):
    """Return the enum member for ``value``, or ``default`` if it is unknown."""
    try:
        return enum_cls(value)
    except ValueError:
        return default


def describe_status(byte):
    """Human-readable text for a status byte (falls back to hex for unknown codes)."""
    try:
        return str(Status(byte))
    except ValueError:
        return f"Unknown (0x{byte:02x})"


# --- Decoded responses --------------------------------------------------------
@dataclass
class Config:
    """Device configuration, decoded from the 10-byte `CONFIG?` payload."""

    dpi: int
    hardware_version: str
    firmware_version: str
    timer: int          # raw 0..3, see TIMER_TEXT
    beep: bool

    def __str__(self):
        return (
            f"DPI: {self.dpi}\n"
            f"Hardware version: {self.hardware_version}\n"
            f"Firmware version: {self.firmware_version}\n"
            f"Auto power-off: {TIMER_TEXT.get(self.timer, self.timer)}\n"
            f"Beep: {'On' if self.beep else 'Off'}"
        )


@dataclass
class Battery:
    """Battery status, decoded from the 2-byte `BATTERY?` payload."""

    percent: int
    charging: bool

    def __str__(self):
        text = f"Battery: {self.percent}% ({'charging' if self.charging else 'not charging'})"
        if self.charging:
            # The printer always reports 99 % while charging.
            text += "\n(Unplug the printer for a real battery reading.)"
        return text


@dataclass
class Label:
    """Loaded label roll, decoded from the 16-byte `ESC ! o` block."""

    width_mm: int
    length_mm: int
    gap_mm: int
    color: Color
    style: Style
    paper_code: str      # the 12 tag bytes as hex, or "" when no tag is readable

    def __str__(self):
        if not self.paper_code:
            return "No readable label tag"
        return (
            f"{self.width_mm}x{self.length_mm} mm, {self.style}, {self.color}, "
            f"gap {self.gap_mm} mm (paper code {self.paper_code})"
        )


# --- Response parsing ---------------------------------------------------------
def crc16(data):
    """CRC16/MODBUS (poly 0xA001, init 0xFFFF) as a 2-byte big-endian digest."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "big")


def _strip(reply, prefix, length):
    """Strip a text reply's ``prefix`` and trailing CRLF, checking the payload length."""
    body = reply[len(prefix):-2]
    if not reply.startswith(prefix) or len(body) != length:
        raise IOError(f"unexpected reply: {reply.hex()}")
    return body


def parse_config(payload):
    """Decode the 10-byte `CONFIG?` payload (see README 'CONFIG payload')."""
    dpi = struct.unpack(">H", payload[0:2])[0]
    return Config(
        dpi=dpi,
        hardware_version=f"{payload[2]}.{payload[3]}.{payload[4]}",
        firmware_version=f"{payload[5]}.{payload[6]}.{payload[7]}",
        timer=payload[8],
        beep=bool(payload[9]),
    )


def parse_battery(payload):
    """Decode the 2-byte `BATTERY?` payload: level in BCD, then a charging flag."""
    level = ((payload[0] >> 4) & 0x0F) * 10 + (payload[0] & 0x0F)
    return Battery(percent=level, charging=bool(payload[1]))


def parse_label(block):
    """Decode the 16-byte `ESC ! o` block (see README 'Label block')."""
    if len(block) != 16 or crc16(block[:14]) != block[14:16]:
        if DEBUG:
            print(f"  (bad/again label block: {block.hex()})")
    fmt = block[3] >> 4                      # tag format version (high nibble of byte 3)
    # byte 7 is the paper type; for format >= 2 its high nibble is cable orientation.
    style_byte = block[7] & 0x0F if fmt >= 2 else block[7]
    tag = block[2:14]
    return Label(
        width_mm=block[13],
        length_mm=block[11],
        # byte 8 is the gap only for tag format >= 2; the stock gapped roll uses 5 mm.
        gap_mm=block[8] if (fmt >= 2 and block[8]) else 5,
        color=_safe_enum(Color, block[4], Color.UNKNOWN),
        style=_safe_enum(Style, style_byte, Style.CONTINUOUS),
        paper_code=tag.hex().upper() if any(tag) else "",
    )


# --- Serial I/O ---------------------------------------------------------------
def open_port(device):
    """Open and cache one serial connection for the whole run.

    The connection is reused because closing an rfcomm tty can tear down the
    underlying Bluetooth link.
    """
    global _port
    if _port is None or not _port.is_open:
        _port = serial.Serial(device, BAUD, timeout=1)
        time.sleep(0.5)
    return _port


def send(device, payload, reply_len=None):
    """Send a command and return the printer's reply.

    ``payload`` may be a str (encoded and CRLF-terminated, for text commands) or
    raw bytes (sent as-is, for ESC sequences and print jobs). ``reply_len``:
      - ``None``: read one CRLF-terminated line  (CONFIG?, BATTERY?)
      - an int:   read exactly that many raw bytes (status 1, label block 16)
      - ``0``:    do not read a reply
    """
    if isinstance(payload, str):
        payload = (payload + "\r\n").encode()
    port = open_port(device)
    port.reset_input_buffer()
    port.write(payload)
    if reply_len is None:
        reply = port.readline()
    elif reply_len == 0:
        reply = b""
    else:
        reply = port.read(reply_len)
    if DEBUG:
        shown = payload[:48] + (b"..." if len(payload) > 48 else b"")
        print(f"  >> {shown!r}\n  << {reply.hex()}")
    return reply


def get_status(device):
    """Return the current printer status byte (`ESC ! ?`)."""
    reply = send(device, b"\x1b!?\r\n", reply_len=1)
    return reply[0] if reply else None


def get_label(device):
    """Return the loaded label roll (`ESC ! o`)."""
    return parse_label(send(device, b"\x1b!o\r\n", reply_len=16))


def get_config(device):
    """Return the device configuration (`CONFIG?`)."""
    return parse_config(_strip(send(device, "CONFIG?"), b"CONFIG ", 10))


def get_battery(device):
    """Return the battery status (`BATTERY?`)."""
    return parse_battery(_strip(send(device, "BATTERY?"), b"BATTERY ", 2))


# --- Image rendering ----------------------------------------------------------
def image_size(label, dpi):
    """Return the print image size in dots for ``label``.

    Width is capped at the 12 mm / 96-dot head and rounded to whole bytes. The
    full label length is printable.
    """
    width = round(min(label.width_mm, HEAD_WIDTH_MM) / 25.4 * dpi / 8) * 8
    height = round(label.length_mm / 25.4 * dpi)
    return width, height


def render_image(path, width, height):
    """Load an image file and return it as the printer's 1-bpp bitmap.

    The data is MSB-first, ``width // 8`` bytes per row, where a 0 bit prints
    black. Pipeline: grayscale, autocontrast, rotate to portrait if needed, scale
    to fit, thermal-bleed correction, centre on white, Floyd-Steinberg dither.
    """
    img = ImageOps.autocontrast(ImageOps.grayscale(Image.open(path)))
    img = ImageEnhance.Contrast(img).enhance(2)
    if img.width > img.height:            # the label is portrait (narrow, tall)
        img = img.rotate(90, expand=True)
    img.thumbnail((width, height), Image.Resampling.NEAREST)

    # Thermal heads over-darken mid-tones; this piecewise curve lifts shadows so
    # the print matches the source. Tuned on a linear ramp to map 16/255 -> 81/255
    # while keeping 0 and 255 fixed (slopes 5.0625 then ~0.728).
    lut = [int(i * 5.0625) if i <= 16 else int(81 + (i - 16) * 0.728) for i in range(256)]
    img = img.point(lut)

    canvas = Image.new("L", (width, height), 255)   # white canvas of the exact size
    canvas.paste(img, ((width - img.width) // 2, (height - img.height) // 2))
    bitmap = canvas.convert("1", dither=Image.Dither.FLOYDSTEINBERG)

    data = bitmap.tobytes()
    return data.ljust((width // 8) * height, b"\xff")   # pad with white if short


def build_test_pattern(width, height):
    """Return a 1-bpp density test pattern: eight bands of fill levels and line grids."""
    image = Image.new("1", (width, height), 1)        # 1 = white
    band = height // 8
    for index in range(8):
        top = index * band
        for y in range(top + 2, top + band - 2):
            for x in range(width):
                match index:
                    case 0: black = True                       # solid black
                    case 1: black = x % 2 == 0 or y % 2 == 0    # ~75 %
                    case 2: black = (x + y) % 2 == 0            # 50 % checkerboard
                    case 3: black = x % 2 == 0 and y % 2 == 0   # ~25 %
                    case 4: black = (y - top) % 2 == 0          # 1 px horizontal lines
                    case 5: black = x % 2 == 0                  # 1 px vertical lines
                    case 6: black = ((y - top) // 2) % 2 == 0   # 2 px horizontal lines
                    case _: black = (x // 2) % 2 == 0           # 2 px vertical lines
                if black:
                    image.putpixel((x, y), 0)
    return image.tobytes()


def build_job(label, image, width, height, density, copies):
    """Build the TSPL print sequence: SIZE, GAP, DIRECTION, DENSITY, CLS, BITMAP, PRINT."""
    header = (
        f"\r\nSIZE {label.width_mm} mm,{label.length_mm} mm\r\n"
        f"GAP {label.gap_mm} mm,0 mm\r\n"
        "DIRECTION 1,1\r\n"
        f"DENSITY {density}\r\n"
        "CLS\r\n"
        f"BITMAP 0,0,{width // 8},{height},1,"
    ).encode()
    return header + image + f"\r\nPRINT {copies}\r\n".encode()


# --- High-level actions -------------------------------------------------------
def do_print(device, args):
    """Render and print an image or the test pattern on the loaded label."""
    status = get_status(device)
    if status != Status.READY:
        print(f"Printer not ready: {describe_status(status)}")
        return

    label = get_label(device)
    if args.label_size:                     # override the tag-reported size
        width_mm, length_mm = (int(v) for v in args.label_size.lower().split("x"))
        label.width_mm, label.length_mm = width_mm, length_mm
    if not label.width_mm or not label.length_mm:
        print("No readable label tag; pass the size with --label-size WxL, e.g. --label-size 14x40.")
        return

    width, height = image_size(label, get_config(device).dpi)
    source = "test pattern" if args.test_pattern else args.image
    print(f"Printing {source} at {width}x{height} dots on a {label.width_mm}x{label.length_mm} mm label")

    image = build_test_pattern(width, height) if args.test_pattern else render_image(args.image, width, height)
    reply = send(device, build_job(label, image, width, height, args.density, args.copies), reply_len=16)

    # A successful job normally gets no reply; a valid 16-byte block carries a status.
    if len(reply) == 16 and crc16(reply[:14]) == reply[14:16] and reply[0] != Status.READY:
        print(f"Printer reported: {describe_status(reply[0])}")
    print("Print job sent.")


def main():
    """Parse arguments and run the requested action."""
    parser = argparse.ArgumentParser(description="Print and manage a Nelko P21/P22 label printer.")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="serial device (default %(default)s)")
    parser.add_argument("--image", help="image file to print on the loaded label")
    parser.add_argument("--test-pattern", action="store_true", help="print a 1-bit density test pattern")
    parser.add_argument("--density", type=int, default=15, help="print darkness 1-15 (default 15)")
    parser.add_argument("--copies", type=int, default=1, help="number of copies (default 1)")
    parser.add_argument("--label-size", metavar="WxL",
                        help="override label size in mm, e.g. 14x40 (needed when no tag is readable)")
    parser.add_argument("--status", action="store_true", help="show the loaded label roll")
    parser.add_argument("--config", action="store_true", help="show DPI, versions, timer and beep")
    parser.add_argument("--battery", action="store_true", help="show the battery level")
    parser.add_argument("--timeout", type=int, choices=[0, 15, 30, 60], help="set auto power-off (minutes)")
    parser.add_argument("--beep", choices=["on", "off"], help="turn the beep on or off")
    parser.add_argument("--selftest", action="store_true", help="print the built-in self-test page")
    parser.add_argument("--debug", action="store_true", help="print the raw bytes sent and received")
    args = parser.parse_args()

    if len(sys.argv) == 1:
        parser.print_help()
        return

    global DEBUG
    DEBUG = args.debug
    device = args.device

    try:
        if args.image or args.test_pattern:
            do_print(device, args)
        elif args.status:
            print(f"Status: {describe_status(get_status(device))}")
            print(f"Label: {get_label(device)}")
        elif args.config:
            print(get_config(device))
        elif args.battery:
            print(get_battery(device))
        elif args.timeout is not None:
            send(device, f"TIMER {chr(MINUTES_TO_TIMER[args.timeout])}", reply_len=0)
            print(get_config(device))
        elif args.beep is not None:
            send(device, f"BEEP {chr(1 if args.beep == 'on' else 0)}", reply_len=0)
            print(get_config(device))
        elif args.selftest:
            print("Printing self-test page...")
            send(device, "SELFTEST", reply_len=0)
    except (serial.SerialException, IOError) as error:
        print(f"Error talking to the printer: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
