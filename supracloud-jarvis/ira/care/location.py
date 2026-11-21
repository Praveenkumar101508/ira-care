"""Where the person is, in plain words.

The watch sends its location ONCE, when an SOS or a suspected fall starts;
never continuously. It is stored on the alert and lives as long as the alert.
"At home" is decided here, from the carer-entered home coordinates and
radius. With no location, or no home coordinates, the honest answer is
"unknown": we never guess.
"""
from __future__ import annotations

import datetime as dt
import math
import os
from dataclasses import dataclass
from typing import Mapping, Optional

EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    accuracy_m: Optional[float] = None
    recorded_at: Optional[dt.datetime] = None


def from_body(body: Optional[Mapping]) -> Optional[Location]:
    """A Location from a request body's `location` object, or None."""
    if not body:
        return None
    lat, lon = body.get("latitude"), body.get("longitude")
    if lat is None or lon is None:
        return None
    return Location(float(lat), float(lon), body.get("accuracy_m"), body.get("recorded_at"))


def from_alert(alert: Mapping) -> Optional[Location]:
    if alert.get("location_latitude") is None or alert.get("location_longitude") is None:
        return None
    return Location(float(alert["location_latitude"]), float(alert["location_longitude"]),
                    alert.get("location_accuracy_m"), alert.get("location_at"))


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def at_home(loc: Optional[Location], person: Mapping) -> Optional[bool]:
    """True within the home radius, False outside it, None when either side is unknown.
    The location's own accuracy widens the radius: a reading that MIGHT be at
    home is not reported as away."""
    if loc is None or person.get("home_latitude") is None or person.get("home_longitude") is None:
        return None
    d = distance_m(loc.latitude, loc.longitude, float(person["home_latitude"]), float(person["home_longitude"]))
    radius = float(person.get("home_radius_m") or 150) + float(loc.accuracy_m or 0)
    return d <= radius


def map_link(loc: Location) -> str:
    """A plain web map link. No key, no paid API."""
    return (f"https://www.openstreetmap.org/?mlat={loc.latitude:.5f}&mlon={loc.longitude:.5f}"
            f"#map=17/{loc.latitude:.5f}/{loc.longitude:.5f}")


def public_url() -> str:
    """Where the alert can be opened. Defaults to the dashboard's development address."""
    return (os.getenv("IRA_CARE_PUBLIC_URL") or "http://localhost:3000").rstrip("/")


def alert_link(alert_id) -> str:
    return f"{public_url()}/alerts/{alert_id}"


def whereabouts(alert: Mapping, person: Mapping) -> str:
    """One sentence for responders: at home or away, the address, the map link,
    or the plain statement that the location is unavailable."""
    loc = from_alert(alert)
    address = (person.get("home_address") or "").strip()
    home = at_home(loc, person) if alert.get("at_home") is None else alert.get("at_home")
    parts: list[str] = []
    if loc is None:
        parts.append("Location unavailable: the watch did not send one.")
        if address:
            parts.append(f"Home address: {address}.")
        return " ".join(parts)
    if home is True:
        parts.append("The person appears to be AT HOME" + (f": {address}." if address else "."))
    elif home is False:
        d = distance_m(loc.latitude, loc.longitude, float(person["home_latitude"]), float(person["home_longitude"]))
        parts.append(f"The person appears to be AWAY FROM HOME, about {_km(d)} from {address or 'home'}.")
    else:
        parts.append("Whether the person is at home is unknown (no home coordinates set)."
                     + (f" Home address: {address}." if address else ""))
    acc = f" (accuracy {int(loc.accuracy_m)} m)" if loc.accuracy_m is not None else ""
    parts.append(f"Location {loc.latitude:.5f}, {loc.longitude:.5f}{acc}: {map_link(loc)}")
    return " ".join(parts)


def _km(metres: float) -> str:
    return f"{metres / 1000:.1f} km" if metres >= 1000 else f"{int(round(metres, -1))} m"
