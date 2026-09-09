"""Reverse geocoding: coordinates in, a place name out.

This is the only part of findpic that touches the network, so it is deliberately
kept small, dependency-free (stdlib ``urllib``), and easy to switch off with
``--no-geocode``.

Two things matter for being a good Nominatim citizen, and both are enforced here:
a descriptive ``User-Agent`` (their policy rejects generic ones) and a hard limit
of one request per second. Results are cached on disk so re-analysing the same
photo, or a batch shot in one place, never re-hits the service.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "findpic/0.1 (photo metadata analyzer; +https://github.com/vyahello/findpic)"
MIN_INTERVAL_SECONDS = 1.0
#: Nominatim answers in about a third of a second. Eight only lengthened the
#: failure case: a dead network cost eight seconds per photograph.
DEFAULT_TIMEOUT = 4.0

#: A reverse-geocode reply is a few kilobytes. The body was read whole, parsed,
#: and written verbatim into the cache and into `place_detail`, so a 25 MB reply
#: landed in ~/.cache for every later run to load, with the report looking
#: entirely normal.
MAX_RESPONSE_BYTES = 256 * 1024

#: Consecutive transport failures before the service is assumed to be gone for
#: this run.
FAILURES_BEFORE_GIVING_UP = 3
#: Nominatim returns names in the local script unless asked otherwise. English
#: keeps reports consistent and greppable; override with --lang.
DEFAULT_LANGUAGE = "en"

#: Address components grouped by tier, most specific first within each tier.
#: We take at most one component per tier, which keeps the one-line place useful
#: ("Louvre, Paris, Ile-de-France, France") instead of letting a house number
#: and a street crowd out the city.
_PLACE_TIERS = (
    ("amenity", "building", "tourism", "shop", "leisure", "road", "neighbourhood"),
    ("village", "town", "city", "municipality", "suburb", "hamlet", "city_district"),
    ("state", "province", "region", "county", "state_district"),
    ("country",),
)


def cache_path() -> Path:
    """Cache location, honouring ``XDG_CACHE_HOME``."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "findpic" / "geocode.json"


class OversizedResponse(Exception):
    """The service sent more than a reverse-geocode reply can legitimately be."""


@dataclass
class Place:
    """A resolved location."""

    display_name: str
    short_name: str
    address: dict[str, Any]
    osm_type: str | None = None
    licence: str | None = None
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "display_name": self.display_name,
            "short_name": self.short_name,
            "address": self.address,
            "osm_type": self.osm_type,
            "licence": self.licence,
            "cached": self.cached,
        }


class Geocoder:
    """Reverse geocoder with an on-disk cache and polite rate limiting."""

    _lock = threading.Lock()
    _last_request = 0.0

    def __init__(
        self,
        enabled: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        cache_file: Path | None = None,
        user_agent: str = USER_AGENT,
        language: str = DEFAULT_LANGUAGE,
    ) -> None:
        self.enabled = enabled
        self.timeout = timeout
        self.user_agent = user_agent
        self.language = language
        self.cache_file = cache_file if cache_file is not None else cache_path()
        self._cache: dict[str, Any] | None = None
        self._dirty = False
        #: Set once the service has told us to stop, or has failed enough times
        #: that asking again only costs the user the timeout. In memory only.
        self._disabled_reason: str | None = None
        self._failures = 0

    # ------------------------------------------------------------------- cache

    def _load_cache(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        try:
            with open(self.cache_file, encoding="utf-8") as handle:
                loaded = json.load(handle)
            self._cache = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._cache = {}
        return self._cache

    def save_cache(self) -> None:
        if not self._dirty or self._cache is None:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash mid-write cannot corrupt the cache.
            temporary = self.cache_file.with_suffix(".tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self._cache, handle)
            temporary.replace(self.cache_file)
            self._dirty = False
        except OSError:
            pass

    def _key(self, latitude: float, longitude: float) -> str:
        # ~11 m of resolution: fine enough to distinguish real locations, coarse
        # enough that near-identical GPS fixes share a cache entry. The language
        # is part of the key because it changes the response.
        return f"{latitude:.4f},{longitude:.4f}@{self.language}"

    # ------------------------------------------------------------------ lookup

    def reverse(self, latitude: float, longitude: float) -> tuple[Place | None, str | None]:
        """Resolve coordinates to a place.

        Returns ``(place, error)``. A network failure is never fatal — the caller
        still has the coordinates, which is the important part.
        """
        if not self.enabled:
            return None, None
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return None, "coordinates out of range"

        # The service has already told us to stop, or has failed enough times
        # that asking again is only costing the user time. Without this, a dead
        # network cost the full timeout *per photograph* — and a local server
        # answering 429 with Retry-After: 3600 received ten more requests, which
        # is a good way to get the user's address banned by the service findpic
        # depends on.
        if self._disabled_reason is not None:
            return None, self._disabled_reason

        key = self._key(latitude, longitude)
        cache = self._load_cache()
        if key in cache:
            payload = cache[key]
            if payload is None:
                return None, "no result (cached)"
            place = self._to_place(payload, cached=True)
            if place is None:
                # A poisoned cache entry must not be believed and must not be
                # kept: the file is plain JSON in ~/.cache and anything may have
                # written it.
                del cache[key]
                self._dirty = True
                return None, "the geocoding service returned a response findpic could not read"
            return place, None

        try:
            payload = self._fetch(latitude, longitude)
        except urllib.error.HTTPError as exc:
            reason = f"geocoding service returned HTTP {exc.code}"
            if exc.code == 429 or exc.code >= 500:
                self._give_up(reason, retry_after=exc.headers.get("Retry-After"))
            return None, reason
        except urllib.error.URLError as exc:
            return None, self._transport_failed(f"geocoding unavailable ({exc.reason})")
        except (TimeoutError, OSError) as exc:
            return None, self._transport_failed(f"geocoding unavailable ({exc})")
        except OversizedResponse as exc:
            return None, f"the geocoding service returned a response findpic could not read ({exc})"
        except json.JSONDecodeError:
            return None, "geocoding service returned invalid JSON"

        self._failures = 0
        if not payload or "error" in payload:
            cache[key] = None
            self._dirty = True
            return None, "no place found for these coordinates"

        place = self._to_place(payload, cached=False)
        if place is None:
            return None, "the geocoding service returned a response findpic could not read"
        cache[key] = payload
        self._dirty = True
        return place, None

    def _transport_failed(self, reason: str) -> str:
        """Count a failure, and stop trying once the network is clearly gone.

        In memory only. A 429 or a dead link is about right now, and writing it
        to the cache would keep place names switched off on the next run too.
        """
        self._failures += 1
        if self._failures >= FAILURES_BEFORE_GIVING_UP:
            self._give_up(reason)
        return reason

    def _give_up(self, reason: str, retry_after: str | None = None) -> None:
        if retry_after:
            reason = f"{reason}, asked to wait {retry_after}s"
        self._disabled_reason = reason

    @property
    def gave_up(self) -> str | None:
        """Why place names stopped, if they did — for the caller to report once."""
        return self._disabled_reason

    def _fetch(self, latitude: float, longitude: float) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {
                "format": "jsonv2",
                "lat": f"{latitude:.7f}",
                "lon": f"{longitude:.7f}",
                "zoom": "18",
                "addressdetails": "1",
            }
        )
        request = urllib.request.Request(
            f"{NOMINATIM_URL}?{query}",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "Accept-Language": self.language,
            },
        )
        self._throttle()
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            # One byte over the ceiling is enough to know it is over.
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise OversizedResponse(f"reply larger than {MAX_RESPONSE_BYTES // 1024} KB")
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        if not isinstance(parsed, dict):
            return {}
        # Only the four fields Place reads, so neither the cache file nor
        # `place_detail` can grow to whatever the service felt like sending.
        return {
            key: parsed[key]
            for key in ("display_name", "address", "osm_type", "licence", "error")
            if key in parsed
        }

    @classmethod
    def _throttle(cls) -> None:
        """Nominatim's usage policy allows at most one request per second."""
        with cls._lock:
            elapsed = time.monotonic() - cls._last_request
            if elapsed < MIN_INTERVAL_SECONDS:
                time.sleep(MIN_INTERVAL_SECONDS - elapsed)
            cls._last_request = time.monotonic()

    # ---------------------------------------------------------------- shaping

    @staticmethod
    def _to_place(payload: dict[str, Any], cached: bool) -> Place | None:
        """Build a Place, or nothing if the reply is not the shape it claims.

        This read `address.get(...)` with no check, so a payload whose `address`
        was a list raised AttributeError and took the whole run with it — and it
        is reachable with no network at all, because cached payloads are
        replayed unvalidated out of a plain JSON file in ~/.cache.
        """
        address = payload.get("address")
        if not isinstance(address, dict):
            return None
        display = payload.get("display_name")
        display = display if isinstance(display, str) else ""
        return Place(
            display_name=display,
            short_name=Geocoder.short_name(address) or display,
            address=address,
            osm_type=payload.get("osm_type") if isinstance(payload.get("osm_type"), str) else None,
            licence=payload.get("licence") if isinstance(payload.get("licence"), str) else None,
            cached=cached,
        )

    @staticmethod
    def short_name(address: dict[str, Any]) -> str:
        """Condense an address dict into a readable one-liner.

        Nominatim returns a dozen fields of varying specificity; printing all of
        them buries the answer. One component per tier keeps place, locality,
        region and country all visible.
        """
        picked: list[str] = []
        for tier in _PLACE_TIERS:
            for component in tier:
                value = address.get(component)
                if value and str(value) not in picked:
                    picked.append(str(value))
                    break
        return ", ".join(picked)
