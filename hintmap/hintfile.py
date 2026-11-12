#!/usr/bin/env python3
"""Reads and writes weather hint files: what a weather model shows at each atlas place for one hour.

A copy of `Tools/SearchBench/scripts/hintfile.py` in the app's repository, which is the original.
The app's decoder is `WeatherHintFile` there; all three agree byte for byte, and each has the same
golden file in its tests (`tests/test_hintfile.py` here). Plain Python 3.9, standard library only.

Layout, little-endian:

    offset size field
    0      4    magic "NSHM"
    4      1    format version (1)
    5      1    header length in bytes (40)
    6      1    body encoding (0 = one byte per place, in atlas order)
    7      1    reserved (0)
    8      8    atlas fingerprint (see `fingerprint`)
    16     4    place count
    20     4    valid time, Unix seconds, a whole hour
    24     4    model run time, Unix seconds
    28     4    time the file was generated, Unix seconds
    32     8    source label, ASCII, zero-padded (for example "gfs0p25")
    40     n    one byte per place

A place's byte:

    bits 0-2  what the model shows at the place: 0 dry, 1 rain, 2 snow, 3 mixed, 4 freezing rain,
              7 no data (5 and 6 are read as no data)
    bit 3     showery: the precipitation is mostly convective
    bits 4-5  nearest rain in the model: 0 none within 100 km, 1 within 100 km, 2 within 30 km
    bits 6-7  nearest snow in the model, same values

Usage:
    hintfile.py dump <file>            header and class counts
    hintfile.py fingerprint <places.tsv>   fingerprint of an atlas (first column = id, with a header line)
"""
import struct
import sys

MAGIC = b"NSHM"
VERSION = 1
ENCODING_BYTE_PER_PLACE = 0
HEADER = struct.Struct("<4sBBBBQIIII8s")
assert HEADER.size == 40

DRY, RAIN, SNOW, MIXED, FREEZING_RAIN, NO_DATA = 0, 1, 2, 3, 4, 7
CLASS_NAMES = {DRY: "dry", RAIN: "rain", SNOW: "snow", MIXED: "mixed", FREEZING_RAIN: "freezing rain", NO_DATA: "no data"}
BEYOND_100_KM, WITHIN_100_KM, WITHIN_30_KM = 0, 1, 2


def fingerprint(ids):
    """FNV-1a 64 over the place ids in atlas order, four little-endian bytes each."""
    value = 0xCBF29CE484222325
    for place_id in ids:
        for byte in struct.pack("<I", place_id):
            value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def cell(place_class, showery=False, rain_band=BEYOND_100_KM, snow_band=BEYOND_100_KM):
    """One place's byte."""
    if place_class not in CLASS_NAMES:
        raise ValueError("unknown class %r" % (place_class,))
    if not 0 <= rain_band <= 2 or not 0 <= snow_band <= 2:
        raise ValueError("a band is 0, 1 or 2")
    return place_class | (8 if showery else 0) | (rain_band << 4) | (snow_band << 6)


def encode(ids, cells, valid, run, generated, source):
    """The bytes of a hint file. Times are Unix seconds; `valid` must be a whole hour."""
    if len(ids) != len(cells):
        raise ValueError("%d ids but %d cells" % (len(ids), len(cells)))
    if valid % 3600:
        raise ValueError("the valid time must be a whole hour")
    label = source.encode("ascii")
    if len(label) > 8:
        raise ValueError("the source label is at most 8 ASCII characters")
    header = HEADER.pack(MAGIC, VERSION, HEADER.size, ENCODING_BYTE_PER_PLACE, 0, fingerprint(ids), len(ids),
                         valid, run, generated, label)
    return header + bytes(cells)


def decode(data):
    """Header fields and the cells of a hint file; raises ValueError on anything unexpected."""
    if len(data) < HEADER.size:
        raise ValueError("shorter than a header")
    magic, version, header_length, encoding, _, atlas, count, valid, run, generated, label = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ValueError("not a hint file")
    if version != VERSION:
        raise ValueError("format version %d" % version)
    if encoding != ENCODING_BYTE_PER_PLACE:
        raise ValueError("body encoding %d" % encoding)
    if header_length < HEADER.size or len(data) != header_length + count:
        raise ValueError("size %d does not fit %d places" % (len(data), count))
    return {"fingerprint": atlas, "count": count, "valid": valid, "run": run, "generated": generated,
            "source": label.rstrip(b"\0").decode("ascii"), "cells": data[header_length:]}


def write(path, ids, cells, valid, run, generated, source):
    with open(path, "wb") as out:
        out.write(encode(ids, cells, valid, run, generated, source))


def read(path):
    with open(path, "rb") as source:
        return decode(source.read())


def atlas_ids(places_path):
    """Place ids of an atlas file (tab-separated, id first, one header line), in file order."""
    ids = []
    with open(places_path, encoding="utf-8") as source:
        next(source)
        for line in source:
            if line.strip():
                ids.append(int(line.split("\t", 1)[0]))
    return ids


def main(arguments):
    if len(arguments) == 2 and arguments[0] == "dump":
        import datetime
        hint = read(arguments[1])
        def stamp(seconds):
            return datetime.datetime.utcfromtimestamp(seconds).strftime("%Y-%m-%dT%H:%MZ")
        print("valid %s | run %s | generated %s | source %s | %d places | atlas %016x" % (
            stamp(hint["valid"]), stamp(hint["run"]), stamp(hint["generated"]), hint["source"], hint["count"], hint["fingerprint"]))
        counts = {}
        for byte in hint["cells"]:
            place_class = byte & 7
            counts[place_class] = counts.get(place_class, 0) + 1
        for place_class in sorted(counts):
            print("  %-13s %6d" % (CLASS_NAMES.get(place_class, "no data"), counts[place_class]))
        near_rain = sum(1 for byte in hint["cells"] if (byte >> 4) & 3 == WITHIN_30_KM)
        near_snow = sum(1 for byte in hint["cells"] if (byte >> 6) & 3 == WITHIN_30_KM)
        print("  rain within 30 km: %d | snow within 30 km: %d" % (near_rain, near_snow))
        return 0
    if len(arguments) == 2 and arguments[0] == "fingerprint":
        ids = atlas_ids(arguments[1])
        print("%016x  %d places" % (fingerprint(ids), len(ids)))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
