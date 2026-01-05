#!/usr/bin/env python3

"""Script to print images and manage settings on a Nelko P21 label printer via serial connection."""

import argparse
import struct
import sys
from enum import IntEnum

import serial
from packaging.version import Version
from PIL import Image, ImageEnhance, ImageOps

DEBUG = False
SERIAL_DEVICE = "/dev/rfcomm0"


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x1:  # If LSB is 1
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    # Convert the 16-bit integer to a 2-byte array in big-endian format.
    return crc.to_bytes(2, byteorder="big")


class DeviceConfig:
    def __init__(self, data):
        self.dpi_resolution = data[0]
        self.hardware_version = Version(f"{data[1]}.{data[2]}.{data[3]}")
        self.second_firmware_version = Version(f"{data[4]}.{data[5]}.{data[6]}")
        self.timeout_setting = TimeoutSetting(data[7])
        self.beep_setting = BeepSetting(data[8])

    def __str__(self):
        return (
            f"DPI Resolution: {self.dpi_resolution}\n"
            f"Hardware Version: {self.hardware_version}\n"
            f"Second Firmware Version: {self.second_firmware_version}\n"
            f"Timeout: {self.timeout_setting}\n"
            f"Beep: {self.beep_setting}"
        )


class PaperType(IntEnum):
    CONTINUOUS = 0
    GAPPED = 1
    BLACKMARK = 2

    def __str__(self):
        match self:
            case PaperType.GAPPED:
                return "Gapped"
            case PaperType.CONTINUOUS:
                return "Continuous"
            case PaperType.BLACKMARK:
                return "Blackmark"
            case _:
                return "Unknown"


class PrinterReadinessStatus(IntEnum):
    READY = 0
    LID_OPEN = 1
    OUT_OF_PAPER = 4
    BUSY = 32

    def __str__(self):
        match self:
            case PrinterReadinessStatus.READY:
                return "Ready"
            case PrinterReadinessStatus.LID_OPEN:
                return "Lid Open"
            case PrinterReadinessStatus.OUT_OF_PAPER:
                return "Paper not loaded"
            case PrinterReadinessStatus.BUSY:
                return "Busy"
            case _:
                return "Unknown"


class PaperColor(IntEnum):
    UNKNOWN = 0
    TRANSPARENT = 2
    WHITE = 3
    PINK = 4
    BLUE = 5
    YELLOW = 6

    def __str__(self):
        match self:
            case PaperColor.TRANSPARENT:
                return "Transparent"
            case PaperColor.WHITE:
                return "White"
            case PaperColor.PINK:
                return "Pink"
            case PaperColor.BLUE:
                return "Blue"
            case PaperColor.YELLOW:
                return "Yellow"
            case _:
                return "Unknown"


def validate_checksum(data):
    # The checksum is the last two bytes of the data.
    provided_checksum = data[-2:]
    # The checksum is computed over the data without the checksum itself.
    computed_checksum = crc16(data[:-2])
    if provided_checksum != computed_checksum:
        raise ValueError(
            f"Invalid checksum: {provided_checksum} != {computed_checksum}"
        )


def get_printer_status():
    status = send_command("\x1b!o\r\n")
    validate_checksum(status)
    return unpack_printer_status(status)


def unpack_printer_status(status):
    unpacked_status = struct.unpack(">BBBBBBBBBBBBBBBB", status)
    return PrinterStatus(unpacked_status)


class PrinterStatus:
    def __init__(self, data):
        self.printer_status = PrinterReadinessStatus(data[0])
        self.data_length = data[1]
        self.data_unknown = data[2]
        self.data_unknown2 = data[3]
        self.label_color = PaperColor(data[4])
        self.border_radius = data[6]  # Maybe padding?
        self.data_unknown3 = data[5]
        self.paper_type = PaperType(data[7])
        self.data_unknown4 = data[8]
        self.data_unknown5 = data[9]
        self.data_unknown6 = data[10]
        self.label_length = data[11]
        self.maximum_label_width = data[12]
        self.label_width = data[13]
        self.data_unknown7 = data[14]

    def __str__(self):
        print_status_str = f"{self.printer_status}\n"
        if self.label_width == 0 and self.label_length == 0:
            print_status_str += "The printer found no readable RFID tag."
        else:
            print_status_str += (
                f"Label Type: {self.label_width}x{self.label_length}mm"
                + f"({self.paper_type}), {self.label_color} color\n"
            )

        if DEBUG:
            print_status_str += (
                f"Data Length: {self.data_length}\n"
                + f"Border Radius ?: {self.border_radius}\n"
                + f"Maximum Label Width?: {self.maximum_label_width}\n"
                + f"Data Unknown 1 (byte 3): {hex(self.data_unknown)}\n"
                + f"Data Unknown 2 (byte 4): {hex(self.data_unknown2)}\n"
                + f"Data Unknown 3 (byte 5): {hex(self.data_unknown3)}\n"
                + f"Data Unknown 4 (byte 8): {hex(self.data_unknown4)}\n"
                + f"Data Unknown 5 (byte 9): {hex(self.data_unknown5)}\n"
                + f"Data Unknown 6 (byte 10): {hex(self.data_unknown6)}\n"
                + f"Data Unknown 7 (byte 15): {hex(self.data_unknown7)}\n"
            )
        return print_status_str


class BatteryData:
    def __init__(self, data):
        # The first byte contains the battery level as BCD (Binary Coded Decimal).
        # We need to convert it to a decimal number by combining the high and low nibbles.
        self.battery_level = ((data[0] >> 4) & 0x0F) * 10 + (data[0] & 0x0F)

        self.charging = data[1]

    def __str__(self):
        class ChargingString:
            def __init__(self, charging):
                self.charging = charging

            def __str__(self):
                match self.charging:
                    case True:
                        return "Charging"
                    case False:
                        return "Not Charging"
                    case _:
                        return "Unknown"

        # The printer always returns 99% charge when plugged.
        if self.charging:
            return (
                f"Battery Level: {self.battery_level}%\n"
                f"Charging: {ChargingString(self.charging)}\n"
                f"Unplug the printer to get a current battery reading."
            )
        else:
            return (
                f"Battery Level: {self.battery_level}%\n"
                f"Charging: {ChargingString(self.charging)}"
            )


class TimeoutSetting(IntEnum):
    NEVER = 0
    MINUTES_15 = 1
    MINUTES_30 = 2
    MINUTES_60 = 3

    def __str__(self):
        match self:
            case TimeoutSetting.NEVER:
                return "Never"
            case TimeoutSetting.MINUTES_15:
                return "15 minutes"
            case TimeoutSetting.MINUTES_30:
                return "30 minutes"
            case TimeoutSetting.MINUTES_60:
                return "60 minutes"
            case _:
                return "Unknown"


class BeepSetting(IntEnum):
    OFF = 0
    ON = 1

    def __str__(self):
        match self:
            case BeepSetting.ON:
                return "On"
            case BeepSetting.OFF:
                return "Off"
            case _:
                return "Unknown"


def load_image(image):
    # Load the image
    image = Image.open(image)
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image)
    enhancer = ImageEnhance.Contrast(image)
    image = enhancer.enhance(2)

    # Rotate the image to its longer side
    if image.width > image.height:
        image = image.rotate(90, expand=True)

    image.thumbnail((96, 284), Image.Resampling.NEAREST)
    image = image.convert("1", dither=Image.Dither.FLOYDSTEINBERG)

    # Convert the image to a bit array
    bitdata = image.tobytes()
    # Pad the image to 3408 bytes, so the printer doesn't fill the rest with black.
    if len(bitdata) < 3408:
        bitdata = bitdata.ljust(3408, b"\xff")

    return bitdata


def get_readiness_status():
    short_status = send_command("\x1b!?")
    unpacked_status = struct.unpack(">B", short_status)
    return PrinterReadinessStatus(unpacked_status[0])


def get_config():
    data = send_command("CONFIG?")
    configdata = clean_serial_response(data, "CONFIG ", 10)
    unpacked_data = struct.unpack(">hBBBBBBB?", configdata)
    return DeviceConfig(unpacked_data)


def get_battery():
    response = send_command("BATTERY?")
    configdata = clean_serial_response(response, "BATTERY ", 2)
    if DEBUG:
        print(f"Battery raw data: {configdata.hex()}")
    unpacked_data = struct.unpack(">B?", configdata)
    return BatteryData(unpacked_data)


def clean_serial_response(response, prefix, expected_len):
    # Cut off the prefix and the CRLF at the end.
    cleaned_response = response[len(prefix) : -2]
    # Validate the response
    if (
        not response.startswith(prefix.encode())
        or len(cleaned_response) != expected_len
    ):
        raise ValueError(f"Invalid response: {response.hex()}")
    return cleaned_response


def get_timeout_command(timeout):
    timeout_setting = TimeoutSetting.NEVER
    match timeout:
        case 0:
            timeout_setting = TimeoutSetting.NEVER
        case 15:
            timeout_setting = TimeoutSetting.MINUTES_15
        case 30:
            timeout_setting = TimeoutSetting.MINUTES_30
        case 60:
            timeout_setting = TimeoutSetting.MINUTES_60
        case _:
            print("Invalid timeout setting. Must be 0, 15, 30 or 60.")
            return

    return f"TIMEOUT {chr(timeout_setting.value)}"


def get_beep_command(beep):
    match beep:
        case True:
            beep_setting = BeepSetting.ON
        case False:
            beep_setting = BeepSetting.OFF
    return f"BEEP {chr(beep_setting.value)}"


def send_command(command, encode=True):
    try:
        with serial.Serial(SERIAL_DEVICE, 115200, timeout=1) as ser:
            if encode:
                ser.write(f"{command}\r\n".encode())
            else:
                ser.write(command)
            response = ser.readline()
            if DEBUG:
                print(f"Received response: {response.hex()}")
            return response
    except serial.SerialException as e:
        print(f"Failed to send data via serial connection: {e}")
        return


def build_print_command(imagedata, density, copies):
    serial_data = b"\x1b!o\r\n"
    serial_data += b"SIZE 14.0 mm,40.0 mm\r\n"
    serial_data += b"GAP 5.0 mm,0 mm\r\n"
    serial_data += b"DIRECTION 1,1\r\n"
    serial_data += f"DENSITY {density}\r\n".encode()
    serial_data += b"CLS\r\n"
    serial_data += b"BITMAP 0,0,12,284,1,"
    serial_data += imagedata
    serial_data += f"\r\nPRINT {copies}\r\n".encode()
    return serial_data


def main():
    parser = argparse.ArgumentParser(
        description="Print an image on a Nelko P21 label printer."
    )
    parser.add_argument(
        "--device",
        help="The device to print to (defaults to /dev/rfcomm0)",
        default="/dev/rfcomm0",
    )
    parser.add_argument("--image", help="The image file to print.")
    parser.add_argument(
        "--density",
        help="The density/darkness of the print (1-15, defaults to 15)",
        type=int,
        default=15,
    )
    parser.add_argument(
        "--copies",
        help="The number of copies to print (defaults to 1)",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--config", help="Get the printer configuration", action="store_true"
    )
    parser.add_argument("--status", help="Get the printer status", action="store_true")
    parser.add_argument(
        "--battery", help="Get the printer battery level", action="store_true"
    )
    parser.add_argument(
        "--timeout", help="Set the printer timeout in minutes (0, 15, 30, 60)", type=int
    )
    parser.add_argument(
        "--beep", help="Enable or disable the printer beep (True, False)", type=bool
    )
    parser.add_argument("--selftest", help="Run a self-test print", action="store_true")
    parser.add_argument("--debug", help="Enable debug output", action="store_true")

    try:
        args = parser.parse_args()
        if len(sys.argv) == 1:
            parser.print_help()
            return
    except (argparse.ArgumentError, argparse.ArgumentTypeError) as e:
        print(f"Failed to parse arguments: {e}")
        parser.print_help()
        return

    if args.device:
        global SERIAL_DEVICE
        SERIAL_DEVICE = args.device
    if args.debug:
        print("Debug mode enabled.")
        global DEBUG
        DEBUG = True
        print(f"Using serial device: {args.device}")
    if args.image:
        bitdata = load_image(args.image)
        print_command = build_print_command(bitdata, args.density, args.copies)
        answer = send_command(print_command, encode=False)
        validate_checksum(answer)
        status = unpack_printer_status(answer)
        if args.debug or status.printer_status != PrinterReadinessStatus.READY:
            print(status)
    if args.config:
        print("Printer configuration:")
        print(get_config())
    if args.battery:
        print("Printer battery status:")
        print(get_battery())
    if args.timeout:
        command = get_timeout_command(args.timeout)
        print(f"Setting timeout to {command} minutes.")
        send_command(command)
        print(get_config())
    if args.beep:
        beep_command = get_beep_command(args.beep)
        send_command(beep_command)
        print(get_config())
    if args.status:
        print(f"Printer status: {get_printer_status()}")
    if args.selftest:
        print("Running self-test print:")
        send_command("SELFTEST")


if __name__ == "__main__":
    main()
