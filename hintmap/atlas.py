"""The app's atlas: the places a hint describes, in the app's order.

A hint file is one byte per place in the order of the app's bundled atlas, so the builder must use
exactly that list. `atlases/<fingerprint>.tsv` holds one file per atlas a released app version
uses (id, latitude, longitude, elevation), exported from the app's `places.tsv` with
`python3 -m hintmap.build atlas`. The fingerprint is the one the app checks before it uses a file.
"""
import os

from . import hintfile


class Place:
    __slots__ = ("id", "latitude", "longitude", "elevation")

    def __init__(self, place_id, latitude, longitude, elevation):
        self.id = place_id
        self.latitude = latitude
        self.longitude = longitude
        self.elevation = elevation


def read(path):
    """Places of an atlas file in file order: the app's `places.tsv` or an exported atlas."""
    places = []
    with open(path, encoding="utf-8") as source:
        header = source.readline().rstrip("\n").split("\t")
        try:
            lat, lon, elevation = header.index("lat"), header.index("lon"), header.index("elevation")
        except ValueError:
            raise ValueError("%s: expected columns id, lat, lon and elevation" % path)
        for line in source:
            if not line.strip():
                continue
            columns = line.rstrip("\n").split("\t")
            places.append(Place(int(columns[0]), float(columns[lat]), float(columns[lon]), float(columns[elevation] or 0)))
    return places


def fingerprint(places):
    return hintfile.fingerprint([place.id for place in places])


def name(places):
    """The folder name of an atlas: its fingerprint in hex."""
    return "%016x" % fingerprint(places)


def export(places_path, folder):
    """Writes `<folder>/<fingerprint>.tsv` from the app's `places.tsv`; returns its path."""
    places = read(places_path)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name(places) + ".tsv")
    with open(path, "w", encoding="utf-8") as out:
        out.write("id\tlat\tlon\televation\n")
        for place in places:
            out.write("%d\t%.5f\t%.5f\t%d\n" % (place.id, place.latitude, place.longitude, int(place.elevation)))
    return path


def all_in(folder):
    """(name, places) of every exported atlas in `folder`, checked against its file name."""
    found = []
    for entry in sorted(os.listdir(folder)):
        if not entry.endswith(".tsv"):
            continue
        places = read(os.path.join(folder, entry))
        if name(places) != entry[:-4]:
            raise ValueError("%s: the places do not match the fingerprint in the file name (%s)" % (entry, name(places)))
        found.append((entry[:-4], places))
    return found
