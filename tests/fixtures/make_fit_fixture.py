"""Write ``short_run.fit``, the FIT fixture the primary Source Adapter is tested against.

A hand-rolled FIT encoder (the FIT SDK has no Python writer, and ``fitdecode`` only reads):
a 14-byte header, one ``file_id`` message, one ``record`` definition, and the data records,
closed by the file CRC. Field numbers, base types, scales and offsets follow the FIT
Profile's ``record`` message (global 20), the same profile ``fitdecode`` decodes with, so
whatever this writes is what a real export looks like on the wire for these fields.

The run itself is synthetic but plausible: 60 fixes 3 s apart, 10 m each along a parallel
of latitude at 48° N, climbing 0.2 m per fix (altitude is stored at 0.2 m resolution,
so the climb is exact on the wire), heart rate ramping 140 → 160. One record
before the first fix carries a timestamp but no position, as a watch records before GPS
lock; the adapter must drop it. ``EXPECTED`` is the ground truth the test reads back.

Run ``python tests/fixtures/make_fit_fixture.py`` to regenerate; commit the result.
"""

import math
import struct
from datetime import datetime, timezone
from pathlib import Path

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc).timestamp()
SEMICIRCLES_PER_DEG = 2**31 / 180.0

START = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
LAT = 48.0
STEP_M = 10.0
STEP_S = 3
CLIMB_M = 0.2
N_FIXES = 60

# The adapter sees exactly these values back (lat/lon within semicircle quantisation).
EXPECTED = {
    "n_points": N_FIXES,
    "start_t": START.timestamp(),
    "step_s": STEP_S,
    "first_ele": 100.0,
    "last_ele": 100.0 + (N_FIXES - 1) * CLIMB_M,
    "first_hr": 140,
    "last_hr": 160,
    "lat": LAT,
}

# (field number, size, base type byte, struct format)
UINT8, UINT16, UINT32, SINT32 = 0x02, 0x84, 0x86, 0x85
ENUM = 0x00
FILE_ID_FIELDS = [(0, 1, ENUM, "B"), (1, 2, UINT16, "H"), (4, 4, UINT32, "I")]
RECORD_FIELDS = [
    (253, 4, UINT32, "I"),  # timestamp, seconds since the FIT epoch
    (0, 4, SINT32, "i"),  # position_lat, semicircles
    (1, 4, SINT32, "i"),  # position_long, semicircles
    (2, 2, UINT16, "H"),  # altitude, scale 5 offset 500
    (78, 4, UINT32, "I"),  # enhanced_altitude, scale 5 offset 500
    (3, 1, UINT8, "B"),  # heart_rate, bpm
    (5, 4, UINT32, "I"),  # distance, scale 100
]
FILE_ID, RECORD = 0, 20
FILE_TYPE_ACTIVITY = 4
MANUFACTURER_COROS = 294
INVALID_SINT32 = 0x7FFFFFFF


def _definition(local: int, global_num: int, fields) -> bytes:
    head = struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(fields))
    return head + b"".join(struct.pack("<BBB", n, size, base) for n, size, base, _ in fields)


def _data(local: int, fields, values) -> bytes:
    return struct.pack("<B" + "".join(f for *_, f in fields), local, *values)


def _crc16(data: bytes, crc: int = 0) -> int:
    table = (0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400)
    for byte in data:
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[byte & 0xF]
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[(byte >> 4) & 0xF]
    return crc


def records() -> bytes:
    fit_start = int(START.timestamp() - FIT_EPOCH)
    lon_step = STEP_M / (111_320 * math.cos(math.radians(LAT)))
    out = _data(1, RECORD_FIELDS, (fit_start - STEP_S, INVALID_SINT32, INVALID_SINT32,
                                   round((100.0 + 500) * 5), round((100.0 + 500) * 5), 140, 0))
    for i in range(N_FIXES):
        ele = 100.0 + i * CLIMB_M
        hr = 140 + round(20 * i / (N_FIXES - 1))
        out += _data(1, RECORD_FIELDS, (
            fit_start + i * STEP_S,
            round(LAT * SEMICIRCLES_PER_DEG),
            round(i * lon_step * SEMICIRCLES_PER_DEG),
            round((ele + 500) * 5),
            round((ele + 500) * 5),
            hr,
            round(i * STEP_M * 100),
        ))
    return out


def build() -> bytes:
    body = (
        _definition(0, FILE_ID, FILE_ID_FIELDS)
        + _data(0, FILE_ID_FIELDS, (FILE_TYPE_ACTIVITY, MANUFACTURER_COROS,
                                    int(START.timestamp() - FIT_EPOCH)))
        + _definition(1, RECORD, RECORD_FIELDS)
        + records()
    )
    header = struct.pack("<BBHI4s", 14, 0x10, 2140, len(body), b".FIT")
    header += struct.pack("<H", _crc16(header))
    return header + body + struct.pack("<H", _crc16(header + body))


FIXTURE = Path(__file__).with_name("short_run.fit")

if __name__ == "__main__":
    FIXTURE.write_bytes(build())
    print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size} bytes)")
