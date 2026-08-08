"""Turn raw measurements into sentences a person can act on.

A photo carries numbers that mean nothing on their own. "GPSImgDirection 291.4"
and "GPSSpeed 0.0446 km/h" are facts, but they are not information — the reader
has to already know what an azimuth is, and has to decide for themselves whether
0.04 km/h counts as moving.

Everything here converts one of those numbers into a claim: which way the camera
was pointing, whether you were standing still, how bright it was, how far away
the subject was. Each returns a catalogue key plus parameters, never a finished
string, so the sentences translate like everything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Note:
    """A rendered-later observation: a catalogue key and its parameters."""

    key: str
    params: dict[str, Any]

    def render(self, translator: Any) -> str:
        return translator.get(self.key, **self.params)


# --------------------------------------------------------------------- where


def describe_accuracy(metres: float | None) -> Note | None:
    """Turn a GPS error radius into something with a felt scale.

    "±65 m" means nothing until you know whether that is a room or a district.
    """
    if metres is None:
        return None
    value = f"{metres:.0f}" if metres >= 10 else f"{metres:.1f}"
    if metres <= 10:
        band = "exact"
    elif metres <= 50:
        band = "building"
    elif metres <= 200:
        band = "block"
    else:
        band = "area"
    return Note(f"detail.accuracy.{band}", {"value": value})


def describe_direction(degrees: float | None, magnetic: bool = False) -> Note | None:
    """Which way the camera was pointing, in words rather than degrees.

    "Azimuth 291°" is jargon. "The camera was pointing west-north-west" is not.
    """
    if degrees is None:
        return None
    points = (
        "n",
        "nne",
        "ne",
        "ene",
        "e",
        "ese",
        "se",
        "sse",
        "s",
        "ssw",
        "sw",
        "wsw",
        "w",
        "wnw",
        "nw",
        "nnw",
    )
    index = int((degrees % 360) / 22.5 + 0.5) % 16
    return Note(
        "detail.direction.magnetic" if magnetic else "detail.direction",
        {"point_key": f"compass.full.{points[index]}", "degrees": f"{degrees:.0f}"},
    )


def describe_movement(speed: float | None, unit: str | None) -> Note | None:
    """Standing still, walking, or in a vehicle.

    A phone's GPS reports drift as motion, so anything under a slow walk is
    reported as stationary rather than as "0.0446 km/h", which invites the
    reader to believe a precision that is not there.
    """
    if speed is None:
        return None
    kmh = speed
    reference = (unit or "").lower()
    if reference.startswith("m") and "mph" in reference:
        kmh = speed * 1.609
    elif reference.startswith("k"):
        kmh = speed
    elif reference.startswith("n"):
        kmh = speed * 1.852

    if kmh < 1.5:
        return Note("detail.movement.still", {})
    if kmh < 7:
        return Note("detail.movement.walking", {"value": f"{kmh:.0f}"})
    if kmh < 25:
        return Note("detail.movement.cycling", {"value": f"{kmh:.0f}"})
    return Note("detail.movement.vehicle", {"value": f"{kmh:.0f}"})


def describe_altitude(metres: float | None, below: bool = False) -> Note | None:
    if metres is None:
        return None
    return Note(
        "detail.altitude.below" if below else "detail.altitude",
        {"value": f"{abs(metres):.0f}"},
    )


# -------------------------------------------------------------------- camera


def describe_light(light_value: float | None) -> Note | None:
    """Reconstruct the lighting from the exposure value.

    LV is a photographic scale most people have never met, but it maps cleanly
    onto situations everybody recognises.
    """
    if light_value is None:
        return None
    if light_value >= 14:
        band = "bright_sun"
    elif light_value >= 12:
        band = "daylight"
    elif light_value >= 9:
        band = "overcast"
    elif light_value >= 7:
        band = "bright_indoor"
    elif light_value >= 5:
        band = "indoor"
    elif light_value >= 2:
        band = "dim"
    else:
        band = "dark"
    return Note(f"detail.light.{band}", {})


def describe_shutter(seconds: float | None, stabilised: bool = False) -> Note | None:
    """Warn when the exposure was long enough to blur without support."""
    if seconds is None or seconds <= 0:
        return None
    if seconds >= 1:
        return Note("detail.shutter.tripod", {"value": _shutter_text(seconds)})
    if seconds >= 1 / 15:
        key = "detail.shutter.slow_stabilised" if stabilised else "detail.shutter.slow"
        return Note(key, {"value": _shutter_text(seconds)})
    if seconds <= 1 / 1000:
        return Note("detail.shutter.freeze", {"value": _shutter_text(seconds)})
    return None


def _shutter_text(seconds: float) -> str:
    if seconds >= 1:
        return f"{seconds:g}"
    return f"1/{round(1 / seconds)}"


def describe_subject_distance(text: str | None) -> Note | None:
    """Apple records how far the lens was focused; that is where the subject was."""
    if not text:
        return None
    numbers = [
        float(part) for part in text.replace("m", "").replace("-", " ").split() if _is_number(part)
    ]
    if not numbers:
        return None
    middle = sum(numbers) / len(numbers)
    if middle < 0.6:
        return Note("detail.distance.macro", {"value": f"{middle:.2f}"})
    if middle > 900:
        return Note("detail.distance.infinity", {})
    value = f"{middle:.1f}" if middle < 10 else f"{middle:.0f}"
    return Note("detail.distance.metres", {"value": value})


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def describe_orientation_at_capture(vector: str | None) -> Note | None:
    """How the phone was held, from the gravity vector Apple stores.

    The three components are gravity in device coordinates, so whichever axis
    dominates tells you the attitude: y for upright, x on its side, z flat.
    """
    if not vector:
        return None
    parts = [float(p) for p in str(vector).replace(",", " ").split() if _is_number(p)]
    if len(parts) != 3:
        return None
    x, y, z = parts
    if abs(z) > 0.8:
        return Note("detail.held.flat_down" if z < 0 else "detail.held.flat_up", {})
    if abs(y) > abs(x):
        return Note("detail.held.upright", {})
    return Note("detail.held.sideways", {})


def aspect_ratio(width: int | None, height: int | None) -> str | None:
    """A familiar ratio like 4:3, reduced from the pixel dimensions."""
    if not width or not height:
        return None

    def gcd(a: int, b: int) -> int:
        while b:
            a, b = b, a % b
        return a

    long_side, short_side = max(width, height), min(width, height)
    divisor = gcd(long_side, short_side) or 1
    ratio_long, ratio_short = long_side // divisor, short_side // divisor
    if ratio_long > 20:
        # Not a recognisable ratio; approximate rather than print 4031:3007.
        return f"{long_side / short_side:.2f}:1"
    return f"{ratio_long}:{ratio_short}"
