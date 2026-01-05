#!/usr/bin/env python3

"""Test script for CRC-16 checksum validation."""

from crc import Calculator, Crc16

def int_to_bytes_low(value):
    """
    Convert a 16-bit integer to a 2-byte array in little-endian format.
    """
    return bytes([(value >> 8) & 0xFF, value & 0xFF])


def get_crc16(data):
    """
    Compute the CRC-16 checksum for the given byte array using polynomial 0xA001.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x1:  # If LSB is 1
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return int_to_bytes_low(crc)


def judge_serial_number_is_ok(byte_array):
    """
    Check if the provided data has a valid CRC-16 checksum.
    """
    if len(byte_array) < 3:
        raise ValueError("Input array must have at least 3 bytes.")

    # Extract the main data and the checksum from the input
    data = byte_array[:-2]
    provided_checksum = byte_array[-2:]  # Last two bytes

    # Compute the CRC-16 checksum for the data
    computed_checksum = get_crc16(data)
    calculator = Calculator(Crc16.IBM)
    # reverse byte order
    data = data[::-1]
    crc = calculator.checksum(data)

    # Compare the computed checksum with the provided checksum
    print(f"Computed checksum: {computed_checksum.hex()}")
    print(f"Provided checksum: {provided_checksum.hex()}")
    print(f"crc16 checksum: {hex(crc)}")
    return computed_checksum == provided_checksum


# Example usage
input_bytes = bytes.fromhex("200c011203000301121215280f0e0d22")

if judge_serial_number_is_ok(input_bytes):
    print("Checksum is valid.")
else:
    print("Checksum is invalid.")
