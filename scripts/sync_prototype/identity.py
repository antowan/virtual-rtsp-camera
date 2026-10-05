"""Diagnostic in-band identities added to copied H.264 access units, not pixels."""

from __future__ import annotations

import struct

IDENTITY_UUID = bytes.fromhex("12331faa842c4272a4457663616d0001")
IDENTITY = struct.Struct("!BIQ")


def identified_packet(data: bytes, view: int, generation: int, global_frame: int) -> bytes:
    payload = IDENTITY_UUID + IDENTITY.pack(view, generation, global_frame)
    # user_data_unregistered SEI followed by rbsp_trailing_bits.
    rbsp = bytes((5, len(payload))) + payload + b"\x80"
    escaped = bytearray()
    zeroes = 0
    for byte in rbsp:
        if zeroes >= 2 and byte <= 3:
            escaped.append(3)
            zeroes = 0
        escaped.append(byte)
        zeroes = zeroes + 1 if byte == 0 else 0
    nal = b"\x06" + escaped
    return len(nal).to_bytes(4, "big") + nal + data


def decoded_identity(frame) -> tuple[int, int, int]:
    identities = [
        bytes(side)
        for side in frame.side_data
        if side.type.name == "SEI_UNREGISTERED" and bytes(side).startswith(IDENTITY_UUID)
    ]
    if len(identities) != 1 or len(identities[0]) != len(IDENTITY_UUID) + IDENTITY.size:
        raise ValueError("decoded frame lacks one complete diagnostic identity")
    return IDENTITY.unpack(identities[0][len(IDENTITY_UUID) :])
