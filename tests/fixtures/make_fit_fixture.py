"""Write ``short_run.fit``, the FIT fixture the primary Source Adapter is tested against.

A hand-rolled FIT encoder (the FIT SDK has no Python writer, and ``fitdecode`` only reads):
a 14-byte header, one ``file_id`` message, one ``record`` definition, and the data messages,
closed by the file CRC. Field numbers, base types, scales and offsets follow the FIT
Profile's ``record`` message (global 20), the same profile ``fitdecode`` decodes with, so
these fields sit on the wire exactly where a real export puts them. The file carries only
``file_id`` and ``record`` messages, so a real export's ``session``/``lap``/``device_info``
messages and developer fields are not represented.

The run itself is synthetic but plausible: 60 trackpoints 3 s apart, 10 m each along a
parallel of latitude at 48° N, climbing 0.2 m each (altitude is stored at 0.2 m resolution,
so the climb is exact on the wire), heart rate ramping 140 → 160. ``points()`` is the ground
truth the tests compare the parsed track against — they read the source values, never the
adapter's own output.

Two details exist to pin adapter behaviour rather than to be realistic:

- One trackpoint before the first GPS lock carries a timestamp but no position, as a watch
  records before it has satellites. The adapter must drop it.
- Every record carries **both** ``altitude`` and ``enhanced_altitude``, as a modern export
  does, and ``build(enhanced=False)`` writes the legacy variant that omits the enhanced
  field, so the adapter's fallback to the plain one is exercised. Both fields carry the same
  truth, because in the FIT Profile ``altitude`` is a *component* of ``enhanced_altitude``:
  a decoder expands the plain field into an ``enhanced_altitude`` value of its own, so a
  record holding two disagreeing altitudes is not a file any device writes, and which one a
  reader sees would come down to field order rather than the adapter's preference.

Run ``python tests/fixtures/make_fit_fixture.py`` to regenerate; commit the result.
"""

import math
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc).timestamp()
SEMICIRCLES_PER_DEG = 2**31 / 180.0

START = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
LAT = 48.0
STEP_M = 10.0
STEP_S = 3
CLIMB_M = 0.2
BASE_ELE_M = 100.0
N_POINTS = 60
HR_START, HR_END = 140, 160


class SourcePoint(NamedTuple):
    """One trackpoint as this generator encodes it — the tests' ground truth."""

    t: float  # unix epoch seconds
    lat: float  # degrees
    lon: float  # degrees
    ele: float  # metres
    hr: int  # bpm


def points() -> list[SourcePoint]:
    lon_step = STEP_M / (111_320 * math.cos(math.radians(LAT)))
    return [
        SourcePoint(
            t=START.timestamp() + i * STEP_S,
            lat=LAT,
            lon=i * lon_step,
            ele=BASE_ELE_M + i * CLIMB_M,
            hr=HR_START + round((HR_END - HR_START) * i / (N_POINTS - 1)),
        )
        for i in range(N_POINTS)
    ]


class Field(NamedTuple):
    """One field in a FIT definition message, and how to pack its value."""

    number: int  # FIT Profile field number within the message
    size: int  # bytes on the wire
    base_type: int  # FIT base type byte
    fmt: str  # struct format character


UINT8, UINT16, UINT32, SINT32, ENUM = 0x02, 0x84, 0x86, 0x85, 0x00

FILE_ID_FIELDS = [Field(0, 1, ENUM, "B"), Field(1, 2, UINT16, "H"), Field(4, 4, UINT32, "I")]

_TIMESTAMP = Field(253, 4, UINT32, "I")  # seconds since the FIT epoch
_POSITION_LAT = Field(0, 4, SINT32, "i")  # semicircles
_POSITION_LONG = Field(1, 4, SINT32, "i")  # semicircles
_ALTITUDE = Field(2, 2, UINT16, "H")  # scale 5, offset 500
_ENHANCED_ALTITUDE = Field(78, 4, UINT32, "I")  # scale 5, offset 500
_HEART_RATE = Field(3, 1, UINT8, "B")  # bpm
_DISTANCE = Field(5, 4, UINT32, "I")  # scale 100

#: A modern export: both altitude fields, the plain one stale.
RECORD_FIELDS = [_TIMESTAMP, _POSITION_LAT, _POSITION_LONG, _ALTITUDE,
                 _ENHANCED_ALTITUDE, _HEART_RATE, _DISTANCE]
#: An older export: only the plain altitude field, carrying the truth.
LEGACY_RECORD_FIELDS = [_TIMESTAMP, _POSITION_LAT, _POSITION_LONG, _ALTITUDE,
                        _HEART_RATE, _DISTANCE]

FILE_ID, RECORD = 0, 20
FILE_TYPE_ACTIVITY = 4
MANUFACTURER_COROS = 294
INVALID_SINT32 = 0x7FFFFFFF
FILE_ID_LOCAL, RECORD_LOCAL = 0, 1


def _fit_seconds(epoch: float) -> int:
    return int(epoch - FIT_EPOCH)


def _altitude(metres: float) -> int:
    """Metres → the FIT altitude encoding shared by both altitude fields (scale 5, offset 500)."""
    return round((metres + 500) * 5)


def _semicircles(degrees: float) -> int:
    return round(degrees * SEMICIRCLES_PER_DEG)


def _definition(local: int, global_num: int, fields: list[Field]) -> bytes:
    head = struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(fields))
    return head + b"".join(struct.pack("<BBB", f.number, f.size, f.base_type) for f in fields)


def _data(local: int, fields: list[Field], values) -> bytes:
    return struct.pack("<B" + "".join(f.fmt for f in fields), local, *values)


def _record(fields: list[Field], point: SourcePoint, distance_m: float, *,
            located: bool = True) -> bytes:
    altitudes = [_altitude(point.ele)] * sum(
        f in (_ALTITUDE, _ENHANCED_ALTITUDE) for f in fields
    )
    return _data(RECORD_LOCAL, fields, (
        _fit_seconds(point.t),
        _semicircles(point.lat) if located else INVALID_SINT32,
        _semicircles(point.lon) if located else INVALID_SINT32,
        *altitudes,
        point.hr,
        round(distance_m * 100),
    ))


def _records(fields: list[Field]) -> bytes:
    track = points()
    first = track[0]
    pre_lock = first._replace(t=first.t - STEP_S)
    out = _record(fields, pre_lock, 0.0, located=False)
    for i, point in enumerate(track):
        out += _record(fields, point, i * STEP_M)
    return out


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


def build(enhanced: bool = True) -> bytes:
    """The fixture's bytes: ``enhanced`` picks the modern or the legacy altitude encoding."""
    fields = RECORD_FIELDS if enhanced else LEGACY_RECORD_FIELDS
    body = (
        _definition(FILE_ID_LOCAL, FILE_ID, FILE_ID_FIELDS)
        + _data(FILE_ID_LOCAL, FILE_ID_FIELDS,
                (FILE_TYPE_ACTIVITY, MANUFACTURER_COROS, _fit_seconds(START.timestamp())))
        + _definition(RECORD_LOCAL, RECORD, fields)
        + _records(fields)
    )
    header = struct.pack("<BBHI4s", 14, 0x10, 2140, len(body), b".FIT")
    header += struct.pack("<H", _crc16(header))
    return header + body + struct.pack("<H", _crc16(header + body))


FIXTURE = Path(__file__).with_name("short_run.fit")

if __name__ == "__main__":
    FIXTURE.write_bytes(build())
    print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size} bytes)")
