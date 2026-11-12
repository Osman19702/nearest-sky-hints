"""Reads and writes cloud files: a weather model's total cloud cover over the globe for one hour.

The Nearest Sky app draws them as a soft cloud layer on its map; its reader is `CloudLayerFile` in
Packages/NearestSkyCore/Sources/NearestSkyCore/Map. Both have the same golden file in their tests.
Plain Python 3.9, standard library only.

Layout, little-endian:

    offset size field
    0      4    magic "NSCL"
    4      1    format version (1)
    5      1    header length in bytes (48)
    6      1    body encoding (0 = one byte per grid cell)
    7      1    reserved (0)
    8      2    grid width (cells along a row, west to east)
    10     2    grid height (rows, north to south)
    12     4    latitude of the first row, micro-degrees (signed)
    16     4    longitude of the first column, micro-degrees (signed)
    20     4    distance between rows, micro-degrees, southwards
    24     4    distance between columns, micro-degrees, eastwards
    28     4    valid time, Unix seconds, a whole hour
    32     4    model run time, Unix seconds
    36     4    time the file was generated, Unix seconds
    40     8    source label, ASCII, zero-padded
    48     n    width x height bytes, row by row: cloud cover in percent (0-100), 255 = no data
"""
import struct

MAGIC = b"NSCL"
VERSION = 1
ENCODING_BYTE_PER_CELL = 0
NO_DATA = 255
HEADER = struct.Struct("<4sBBBBHHiiIIIII8s")
assert HEADER.size == 48


def micro(degrees):
    return int(round(degrees * 1_000_000))


def encode(cells, width, height, first_latitude, first_longitude, latitude_step, longitude_step, valid, run, generated, source):
    """The bytes of a cloud file. Times are Unix seconds; `valid` must be a whole hour."""
    if len(cells) != width * height:
        raise ValueError("%d cells for a %d x %d grid" % (len(cells), width, height))
    if not (0 < width < 65536 and 0 < height < 65536):
        raise ValueError("grid size out of range")
    if latitude_step <= 0 or longitude_step <= 0:
        raise ValueError("steps must be positive")
    if valid % 3600:
        raise ValueError("the valid time must be a whole hour")
    if any(value > 100 and value != NO_DATA for value in cells):
        raise ValueError("cover must be 0-100 or %d" % NO_DATA)
    label = source.encode("ascii")
    if len(label) > 8:
        raise ValueError("the source label is at most 8 ASCII characters")
    header = HEADER.pack(MAGIC, VERSION, HEADER.size, ENCODING_BYTE_PER_CELL, 0, width, height, micro(first_latitude), micro(first_longitude),
                         micro(latitude_step), micro(longitude_step), valid, run, generated, label)
    return header + bytes(cells)


def decode(data):
    """Header fields and cells of a cloud file; raises ValueError on anything unexpected."""
    if len(data) < HEADER.size:
        raise ValueError("shorter than a header")
    (magic, version, header_length, encoding, _, width, height, latitude, longitude, latitude_step, longitude_step,
     valid, run, generated, label) = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ValueError("not a cloud file")
    if version != VERSION:
        raise ValueError("format version %d" % version)
    if encoding != ENCODING_BYTE_PER_CELL:
        raise ValueError("body encoding %d" % encoding)
    if header_length < HEADER.size or width == 0 or height == 0 or len(data) != header_length + width * height:
        raise ValueError("size %d does not fit a %d x %d grid" % (len(data), width, height))
    return {"width": width, "height": height, "first_latitude": latitude / 1e6, "first_longitude": longitude / 1e6,
            "latitude_step": latitude_step / 1e6, "longitude_step": longitude_step / 1e6, "valid": valid, "run": run,
            "generated": generated, "source": label.rstrip(b"\0").decode("ascii"), "cells": data[header_length:]}


def halve(values, width, height):
    """A global latitude/longitude grid at half the resolution, sampled on every second point with
    a 1-2-1 smoothing filter (longitude wraps around, latitude is clamped at the poles).

    `values` holds `height` rows of `width` cells, the first row at the north pole and the first
    column at 0 degrees; `width` must be even and `height` odd, as for GFS 0.25 (1440 x 721), which
    gives 720 x 361 on a 0.5 degree grid with the same first row and column.
    """
    if width % 2 or height % 2 == 0:
        raise ValueError("expected an even width and an odd height")
    out_width, out_height = width // 2, (height + 1) // 2
    weights = (1.0, 2.0, 1.0)
    result = []
    for row in range(out_height):
        source_row = 2 * row
        rows = [max(0, min(height - 1, source_row + offset)) for offset in (-1, 0, 1)]
        for column in range(out_width):
            source_column = 2 * column
            total = 0.0
            weight_sum = 0.0
            for row_weight, r in zip(weights, rows):
                base = r * width
                for column_weight, offset in zip(weights, (-1, 0, 1)):
                    value = values[base + (source_column + offset) % width]
                    if value != value:  # NaN: no data
                        continue
                    w = row_weight * column_weight
                    total += w * value
                    weight_sum += w
            result.append(total / weight_sum if weight_sum else float("nan"))
    return result, out_width, out_height


def quantise(values):
    """Percent cover as bytes: rounded and clamped to 0-100, no data as 255."""
    cells = bytearray(len(values))
    for index, value in enumerate(values):
        if value != value:
            cells[index] = NO_DATA
        else:
            cells[index] = max(0, min(100, int(round(value))))
    return cells
