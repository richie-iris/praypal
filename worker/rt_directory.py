"""rt_directory.py — find a real phone number for a real place.

One entry point, several sources, tried in order of how much they can be trusted:

  1. NPI Registry — the US government's list of every licensed healthcare
     provider. Free, no key, authoritative. Doctors, dentists, therapists,
     home health, pharmacies. This is the source that matters most: older
     adults phone their doctor more than anything else.
  2. Google Places — everything else, with "open now". Used only if the key
     has Places enabled; a 401 just moves on.
  3. OpenStreetMap — restaurants, clubs, senior centres, libraries, places of
     worship, shops. Free and surprisingly good on phone numbers, but the
     public Overpass endpoint is flaky, so it gets a short leash.
  4. Grounded web search — the existing fallback, which always answers.

Everything here runs DURING a live call, so every source has a hard timeout and
fails soft. A slow lookup is a worse failure than a missing one: silence on the
line is what an 80-year-old experiences as "it's broken".
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

import rt_obs
from rt_logger import get_logger
from rt_patterns import CLINICAL as _CLINICAL

log = get_logger("rt_directory")
_obs = rt_obs.get("rt_directory")
_UA = {"User-Agent": "perennial-companion/1.0 (voice companion for anyone who wants one)"}


_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
           "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
           "WI WY DC").split()
_STATE_NAMES = {
    "new jersey": "NJ", "new york": "NY", "pennsylvania": "PA", "connecticut": "CT",
    "california": "CA", "florida": "FL", "texas": "TX", "massachusetts": "MA",
    "illinois": "IL", "ohio": "OH", "michigan": "MI", "georgia": "GA",
    "north carolina": "NC", "virginia": "VA", "arizona": "AZ", "washington": "WA",
    "maryland": "MD", "colorado": "CO", "delaware": "DE", "rhode island": "RI",
}


def _net_where(url: str) -> dict:
    """host and path only. The query string carries the caller's search text —
    a doctor's name, her town — so it never reaches the log stream."""
    try:
        parts = urllib.parse.urlsplit(url or "")
        return {"host": parts.hostname or "", "path": parts.path or "/"}
    except Exception as _exc:
        _obs.caught("rt_directory._net_where", _exc)
        return {"host": "", "path": ""}


def _net_ok(url: str, t0: float, status, raw) -> None:
    with contextlib.suppress(Exception):
        _obs.event("net.request",
                   ms=round((time.perf_counter() - t0) * 1000, 1),
                   status=status, bytes=len(raw or b""), **_net_where(url))


def _net_failed(url: str, t0: float, exc: BaseException) -> None:
    with contextlib.suppress(Exception):
        name = type(exc).__name__
        code = getattr(exc, "code", None)
        _obs.event("net.failed",
                   ms=round((time.perf_counter() - t0) * 1000, 1),
                   err=f"{name} {code}" if code else name, **_net_where(url))


def _get(url: str, timeout: float) -> dict | list:
    # Callers pass https literals; anything else (file:, ftp:) is a bug, not a lookup.
    if not url.startswith("https://"):
        raise ValueError(f"rt_directory: refusing non-https URL {url[:40]!r}")
    req = urllib.request.Request(url, headers=_UA)  # noqa: S310 - https enforced above
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https enforced above
            raw = r.read()
            status = getattr(r, "status", None)
    except Exception as e:
        _obs.caught("rt_directory._get", e)
        _net_failed(url, t0, e)
        raise
    _net_ok(url, t0, status, raw)
    return json.loads(raw.decode())


def _e164(raw: str | None) -> str | None:
    import rt_prefs
    return rt_prefs.normalize_e164(raw or "")


def _parse_place(text: str) -> tuple[str, str | None, str | None]:
    """Split 'Dr Bergman cardiologist in Sparta, New Jersey' → (who, city, state)."""
    t = " ".join((text or "").split())
    state = None
    low = t.lower()
    for name, code in _STATE_NAMES.items():
        if name in low:
            state = code
            break
    if not state:
        m = re.search(r"\b(" + "|".join(_STATES) + r")\b", t)
        if m:
            state = m.group(1)
    city = None
    subject = t
    m = re.search(r"\b(?:in|near|around|at|from)\s+([A-Z][a-zA-Z.\- ]{2,30})", t)
    if m:
        city = m.group(1).strip().rstrip(",")
        for name in list(_STATE_NAMES) + [s.lower() for s in _STATES]:
            city = re.sub(rf"[,\s]+{re.escape(name)}$", "", city, flags=re.I).strip()
        subject = t[:m.start()].strip()
    for name in list(_STATE_NAMES):
        subject = re.sub(rf"\b{re.escape(name)}\b", "", subject, flags=re.I)
    subject = re.sub(r"[,\s]+$", "", subject).strip()
    return (subject or t), (city or None), state


def npi(query: str, timeout: float = 6.0) -> list[dict]:
    """Look up a licensed provider. Free, official, no key."""
    if not _CLINICAL.search(query):
        return []
    who, city, state = _parse_place(query)
    names = [w for w in re.findall(r"[A-Z][a-z]{2,}", who)
             if w.lower() not in ("dr", "doctor", "the", "office", "for", "find")]
    if not names:
        return []
    last = names[-1]
    first = names[0] if len(names) > 1 else None
    params = {"version": "2.1", "last_name": last, "limit": "10"}
    if state:
        params["state"] = state
    try:
        data = _get("https://npiregistry.cms.hhs.gov/api/?" + urllib.parse.urlencode(params), timeout)
    except Exception as e:
        print(f"[rt-dir] npi failed: {str(e)[:80]}", flush=True)
        return []

    out = []
    for r in (data.get("results") or []):
        b = r.get("basic") or {}
        addr = next((a for a in (r.get("addresses") or [])
                     if a.get("address_purpose") == "LOCATION"), {})
        num = _e164(addr.get("telephone_number"))
        if not num:
            continue
        full = " ".join(x for x in (b.get("first_name"), b.get("last_name")) if x).title()
        tax = (r.get("taxonomies") or [{}])[0].get("desc") or ""
        score = 0
        if first and (b.get("first_name") or "").lower().startswith(first.lower()):
            score += 2
        if city and (addr.get("city") or "").lower() == city.lower():
            score += 1
        out.append({
            "name": full or (b.get("organization_name") or last).title(),
            "number": num, "source": "the federal provider registry",
            "confidence": "high", "detail": tax,
            "where": f"{(addr.get('city') or '').title()}, {addr.get('state') or ''}".strip(", "),
            "_score": score,
        })
    out.sort(key=lambda x: -x["_score"])
    return out[:4]


def places(query: str, timeout: float = 6.0) -> list[dict]:
    key = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    if not key:
        return []
    url = "https://places.googleapis.com/v1/places:searchText"
    t0 = time.perf_counter()
    answered = False
    try:
        req = urllib.request.Request(  # noqa: S310 - fixed https Places URL
            url,
            data=json.dumps({"textQuery": query, "maxResultCount": 4}).encode(),
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                     "X-Goog-FieldMask": ("places.displayName,places.nationalPhoneNumber,"
                                          "places.formattedAddress,places.currentOpeningHours.openNow")},
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https Places URL
            raw = r.read()
            status = getattr(r, "status", None)
        answered = True
        _net_ok(url, t0, status, raw)
        data = json.loads(raw.decode())
    except Exception as e:
        if not answered:
            _net_failed(url, t0, e)
        print(f"[rt-dir] places unavailable: {str(e)[:60]}", flush=True)
        return []
    out = []
    for p in (data.get("places") or []):
        num = _e164(p.get("nationalPhoneNumber"))
        if not num:
            continue
        open_now = (p.get("currentOpeningHours") or {}).get("openNow")
        out.append({"name": (p.get("displayName") or {}).get("text") or query,
                    "number": num, "source": "Google's business listing",
                    "confidence": "high",
                    "detail": ("open now" if open_now else "closed right now") if open_now is not None else "",
                    "where": (p.get("formattedAddress") or "")[:60]})
    return out


_OSM_KINDS = {
    "restaurant": '["amenity"~"restaurant|fast_food|cafe|bar|pub"]',
    "pharmacy": '["amenity"="pharmacy"]',
    "club": '["amenity"~"community_centre|social_centre|social_facility"]',
    "worship": '["amenity"="place_of_worship"]',
    "library": '["amenity"="library"]',
    "bank": '["amenity"="bank"]',
    "post": '["amenity"="post_office"]',
    "shop": '["shop"]',
    "hair": '["shop"~"hairdresser|beauty"]',
    "vet": '["amenity"="veterinary"]',
    "town": '["amenity"~"townhall|police|fire_station"]',
}

_KIND_WORDS = [
    ("restaurant", r"restaurant|diner|pizza|pizzeria|cafe|coffee|bar\b|pub\b|eatery|takeout|deli"),
    ("club", r"\bclub\b|community cent|senior cent|legion|elks|rotary|vfw|social"),
    ("worship", r"church|synagogue|temple|mosque|parish|congregation"),
    ("library", r"librar"),
    ("pharmacy", r"pharmac|drug ?store|walgreens|cvs|rite aid"),
    ("bank", r"\bbank\b|credit union"),
    ("post", r"post office"),
    ("hair", r"salon|barber|hairdress"),
    ("vet", r"\bvet\b|veterinar|animal hospital"),
    ("town", r"town hall|city hall|police|fire department|municipal"),
]


def _geocode(place: str, timeout: float = 4.0) -> tuple[float, float] | None:
    try:
        d = _get("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": place, "format": "json", "limit": 1}), timeout)
        if d:
            return float(d[0]["lat"]), float(d[0]["lon"])
    except Exception as e:
        print(f"[rt-dir] geocode failed: {str(e)[:60]}", flush=True)
    return None


def osm(query: str, timeout: float = 8.0) -> list[dict]:
    """Local places. Free, but the public endpoint is flaky — short leash, fail soft."""
    who, city, state = _parse_place(query)
    if not city:
        return []
    low = who.lower()
    kind = next((k for k, pat in _KIND_WORDS if re.search(pat, low)), "shop")
    here = _geocode(f"{city}, {state or 'USA'}")
    if not here:
        return []
    lat, lon = here
    name_bit = ""
    m = re.search(r"([A-Z][\w'&.\-]+(?: [A-Z][\w'&.\-]+)*)", who)
    if m and len(m.group(1)) > 3 and not _CLINICAL.search(m.group(1)):
        name_bit = f'["name"~"{re.escape(m.group(1).split()[0])}",i]'
    sel = _OSM_KINDS.get(kind, '["shop"]')
    q = (f'[out:json][timeout:{int(timeout)}];('
         f'node{sel}{name_bit}(around:12000,{lat},{lon});'
         f'way{sel}{name_bit}(around:12000,{lat},{lon}););out center 8;')
    try:
        d = _get("https://overpass-api.de/api/interpreter?" + urllib.parse.urlencode({"data": q}), timeout)
    except Exception as e:
        print(f"[rt-dir] osm unavailable: {str(e)[:60]}", flush=True)
        return []
    out = []
    for el in (d.get("elements") or []):
        t = el.get("tags") or {}
        num = _e164(t.get("phone") or t.get("contact:phone"))
        if not num or not t.get("name"):
            continue
        street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
        out.append({"name": t["name"], "number": num,
                    "source": "the local business listing",
                    "confidence": "medium", "detail": t.get("cuisine") or kind,
                    "where": street or city})
    return out[:4]


def lookup(query: str, gemini_json=None, api_key: str = "") -> dict:
    """Best available number for `query`, or {} when nothing trustworthy turns up.

    Ordered by trust: the provider registry, then Google, then OpenStreetMap,
    then the grounded web search the agent already had.
    """
    query = (query or "").strip()
    if not query:
        return {}

    tiers: list = []
    if _CLINICAL.search(query):
        tiers = [npi, places, osm]
    else:
        tiers = [places, osm, npi]

    for fn in tiers:
        try:
            hits = fn(query)
        except Exception as e:
            print(f"[rt-dir] {fn.__name__} error: {str(e)[:70]}", flush=True)
            continue
        if hits:
            best = hits[0]
            best["alternatives"] = [
                {k: h[k] for k in ("name", "number", "where", "detail")} for h in hits[1:3]]
            print(f"[rt-dir] {fn.__name__}: {best['name']} → {best['number']} "
                  f"({best['source']})", flush=True)
            with contextlib.suppress(Exception):
                _obs.event("directory.lookup_detail",
                           candidates=len(hits),
                           confidence=best.get("confidence") or "medium",
                           source=best.get("source") or fn.__name__)
            return best

    if gemini_json and api_key:
        import rt_bridge
        res = rt_bridge.lookup_number(query, gemini_json, api_key)
        if res.get("number"):
            res.setdefault("detail", "")
            res.setdefault("where", "")
            res.setdefault("alternatives", [])
            with contextlib.suppress(Exception):
                _obs.event("directory.lookup_detail",
                           candidates=1,
                           confidence=res.get("confidence") or "medium",
                           source="gemini_bridge")
            return res
        with contextlib.suppress(Exception):
            _obs.event("directory.lookup_detail", candidates=0, confidence="none", source="none")
        return res or {}
    with contextlib.suppress(Exception):
        _obs.event("directory.lookup_detail", candidates=0, confidence="none", source="none")
    return {}
