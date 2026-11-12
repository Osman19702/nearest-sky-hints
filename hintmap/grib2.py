"""A small GRIB2 reader: enough to decode the fields of NOAA's GFS that the hint map needs.

GRIB2 is the format weather centres publish model fields in. This module reads messages on a
regular latitude/longitude grid whose values are packed with "simple packing" (data representation
template 5.0), "complex packing" (5.2) or "complex packing and spatial differencing" (5.3), which
is what the GFS 0.25 degree files use. Anything else raises `GribError`: the builder must fail
loudly rather than publish a map decoded by guesswork.

Plain Python (3.9 or newer), standard library only, so the builder has no native dependency to
install or to drift. A million-point field takes a second or two.

Reference: WMO Manual on Codes, FM 92 GRIB edition 2 (sections 0 to 8; templates 3.0, 4.0, 4.8,
5.0, 5.2, 5.3), and NCEP's g2c library (`comunpack`) for the order of the packed parts.
"""
from itertools import accumulate
import struct


class GribError(ValueError):
    """The bytes are not a GRIB2 message this reader understands."""


def _uint(data, offset, size):
    return int.from_bytes(data[offset:offset + size], "big")


def _sint(data, offset, size):
    """A sign-and-magnitude integer, as GRIB stores negative numbers."""
    value = int.from_bytes(data[offset:offset + size], "big")
    sign = 1 << (size * 8 - 1)
    return -(value & (sign - 1)) if value & sign else value


def unpack_bits(data, bit_offset, count, width):
    """`count` unsigned integers of `width` bits each, starting `bit_offset` bits into `data`."""
    if count == 0:
        return []
    if width == 0:
        return [0] * count
    end_bit = bit_offset + count * width
    if end_bit > len(data) * 8:
        raise GribError("the data section is shorter than its header says")
    values = []
    append = values.append
    mask = (1 << width) - 1
    # Work through the stream in pieces of a few hundred bits, so the integers stay small.
    per_piece = max(1, 512 // width)
    position = bit_offset
    remaining = count
    while remaining:
        take = per_piece if remaining > per_piece else remaining
        first_byte = position >> 3
        last_bit = position + take * width
        last_byte = (last_bit + 7) >> 3
        piece = int.from_bytes(data[first_byte:last_byte], "big")
        shift = (last_byte << 3) - position - width
        for _ in range(take):
            append((piece >> shift) & mask)
            shift -= width
        position = last_bit
        remaining -= take
    return values


class Message:
    """One GRIB2 message: what it describes and its packed values."""

    def __init__(self, discipline):
        self.discipline = discipline
        self.reference_time = None        # (year, month, day, hour, minute, second), UTC
        self.grid_template = None
        self.ni = self.nj = 0
        self.la1 = self.lo1 = self.la2 = self.lo2 = self.di = self.dj = 0.0
        self.scanning_mode = 0
        self.product_template = None
        self.category = self.number = None
        self.forecast_time = None         # in `time_unit` (1 = hours)
        self.time_unit = None
        self.data_points = 0
        self.packing_template = None
        self.packing = b""
        self.bitmap_indicator = 255
        self.data = b""

    def describe(self):
        return "discipline %s category %s number %s, +%s (unit %s), %d x %d, packing 5.%s" % (
            self.discipline, self.category, self.number, self.forecast_time, self.time_unit, self.ni, self.nj, self.packing_template)

    def index_of(self, latitude, longitude):
        """Position in `values()` of the grid point nearest to a coordinate."""
        if self.grid_template != 0 or self.scanning_mode != 0:
            raise GribError("only grids that run west to east and north to south are supported")
        row = int(round((self.la1 - latitude) / self.dj))
        column = int(round(((longitude - self.lo1) % 360.0) / self.di)) % self.ni
        row = min(self.nj - 1, max(0, row))
        return row * self.ni + column

    def values(self):
        """The field as a list of floats, in the grid's scanning order."""
        if self.bitmap_indicator != 255:
            raise GribError("messages with a bitmap are not supported")
        packing = self.packing
        reference = struct.unpack(">f", packing[0:4])[0]
        binary_scale = 2.0 ** _sint(packing, 4, 2)
        decimal_scale = 10.0 ** (-_sint(packing, 6, 2))
        bits = packing[8]
        count = self.data_points
        if self.packing_template == 0:
            integers = unpack_bits(self.data, 0, count, bits)
        elif self.packing_template in (2, 3):
            integers = self._complex(packing, bits, count)
        else:
            raise GribError("data representation template 5.%s is not supported" % self.packing_template)
        if len(integers) != count:
            raise GribError("decoded %d values, expected %d" % (len(integers), count))
        if binary_scale == 1.0:
            return [(reference + value) * decimal_scale for value in integers]
        return [(reference + value * binary_scale) * decimal_scale for value in integers]

    def _complex(self, packing, bits, count):
        missing_management = packing[11]
        if missing_management != 0:
            raise GribError("complex packing with missing values is not supported")
        groups = _uint(packing, 20, 4)
        width_reference = packing[24]
        width_bits = packing[25]
        length_reference = _uint(packing, 26, 4)
        length_increment = packing[30]
        last_length = _uint(packing, 31, 4)
        length_bits = packing[35]
        data = self.data
        position = 0
        order = 0
        first = second = minimum = 0
        if self.packing_template == 3:
            order = packing[36]
            extra = packing[37]
            if order not in (1, 2):
                raise GribError("spatial differencing of order %d is not supported" % order)
            if extra:
                first = _sint(data, 0, extra)
                if order == 2:
                    second = _sint(data, extra, extra)
                minimum = _sint(data, extra * order, extra)
                position = extra * (order + 1) * 8
        if groups == 0:
            return [0] * count if count else []

        def aligned(bit):
            return (bit + 7) & ~7

        references = unpack_bits(data, position, groups, bits)
        position = aligned(position + groups * bits)
        widths = unpack_bits(data, position, groups, width_bits)
        position = aligned(position + groups * width_bits)
        lengths = unpack_bits(data, position, groups, length_bits)
        position = aligned(position + groups * length_bits)
        lengths = [length_reference + stored * length_increment for stored in lengths]
        lengths[-1] = last_length
        if sum(lengths) != count:
            raise GribError("the groups hold %d values, expected %d" % (sum(lengths), count))

        values = []
        extend = values.extend
        for group in range(groups):
            width = widths[group] + width_reference
            length = lengths[group]
            base = references[group]
            if width == 0:
                extend([base] * length)
                continue
            first_byte = position >> 3
            last_bit = position + length * width
            last_byte = (last_bit + 7) >> 3
            piece = int.from_bytes(data[first_byte:last_byte], "big")
            shift = (last_byte << 3) - position - width
            mask = (1 << width) - 1
            if length <= 64:
                extend([base + ((piece >> (shift - index * width)) & mask) for index in range(length)])
            else:
                # A long group: cut it up so that the shifted integer stays small.
                extend(base + value for value in unpack_bits(data, position, length, width))
            position = last_bit
        if position > len(data) * 8:
            raise GribError("the data section is shorter than its groups")

        if order == 1:
            steps = [first] + [value + minimum for value in values[1:]]
            return list(accumulate(steps))
        if order == 2:
            if count < 2:
                return [first][:count]
            # Second differences back to first differences, then to the values.
            slopes = list(accumulate([second - first] + [value + minimum for value in values[2:]]))
            return list(accumulate([first] + slopes))
        return values


def messages(data):
    """Yields every `Message` in `data` (the bytes of one or more GRIB2 messages)."""
    offset = 0
    total = len(data)
    while offset < total:
        if data[offset:offset + 4] != b"GRIB":
            # Byte-range downloads can carry a few stray bytes between messages.
            following = data.find(b"GRIB", offset)
            if following < 0:
                return
            offset = following
        if offset + 16 > total:
            raise GribError("truncated message header")
        if data[offset + 7] != 2:
            raise GribError("GRIB edition %d is not supported" % data[offset + 7])
        length = _uint(data, offset + 8, 8)
        end = offset + length
        if end > total or data[end - 4:end] != b"7777":
            raise GribError("truncated or damaged message")
        message = Message(data[offset + 6])
        position = offset + 16
        while position < end - 4:
            section_length = _uint(data, position, 4)
            number = data[position + 4]
            if section_length < 5 or position + section_length > end - 4:
                raise GribError("damaged section %d" % number)
            section = data[position:position + section_length]
            if number == 1:
                message.reference_time = (_uint(section, 12, 2), section[14], section[15], section[16], section[17], section[18])
            elif number == 3:
                message.grid_template = _uint(section, 12, 2)
                if message.grid_template != 0:
                    raise GribError("grid definition template 3.%d is not supported" % message.grid_template)
                message.ni = _uint(section, 30, 4)
                message.nj = _uint(section, 34, 4)
                basic_angle = _uint(section, 38, 4)
                subdivisions = _uint(section, 42, 4)
                unit = 1e-6 if basic_angle == 0 or subdivisions in (0, 0xFFFFFFFF) else basic_angle / float(subdivisions)
                message.la1 = _sint(section, 46, 4) * unit
                message.lo1 = _sint(section, 50, 4) * unit
                message.la2 = _sint(section, 55, 4) * unit
                message.lo2 = _sint(section, 59, 4) * unit
                message.di = _uint(section, 63, 4) * unit
                message.dj = _uint(section, 67, 4) * unit
                message.scanning_mode = section[71]
            elif number == 4:
                message.product_template = _uint(section, 7, 2)
                message.category = section[9]
                message.number = section[10]
                if message.product_template in (0, 1, 2, 8, 11, 12):
                    message.time_unit = section[17]
                    message.forecast_time = _uint(section, 18, 4)
            elif number == 5:
                message.data_points = _uint(section, 5, 4)
                message.packing_template = _uint(section, 9, 2)
                message.packing = section[11:]
            elif number == 6:
                message.bitmap_indicator = section[5]
            elif number == 7:
                message.data = section[5:]
                yield message
                # A message may repeat sections 2 to 7 for further fields on the same grid.
                fresh = Message(message.discipline)
                for name in ("reference_time", "grid_template", "ni", "nj", "la1", "lo1", "la2", "lo2", "di", "dj", "scanning_mode",
                             "product_template", "category", "number", "forecast_time", "time_unit", "data_points", "packing_template",
                             "packing", "bitmap_indicator"):
                    setattr(fresh, name, getattr(message, name))
                message = fresh
            position += section_length
        offset = end
