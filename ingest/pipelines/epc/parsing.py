"""EPC row parsing + address normalisation.

The normalisation output (norm_postcode / paon_number / paon_name / saon_token /
norm_street) MUST mirror the PPD-side SQL in postgres/init/03_epc_schema.sql
(transaction_epc) so the two sides join. PPD ships clean paon/saon/street
columns; EPC ships free-text ADDRESS1/2/3, so we parse them into the same shape.
"""
import re
from datetime import datetime


# Columns we persist (order matches the COPY / upsert column list in the ingestor).
COLS = [
    "lmk_key", "uprn", "uprn_source", "building_reference_number",
    "address1", "address2", "address3", "postcode",
    "current_energy_rating", "current_energy_efficiency",
    "property_type", "built_form", "transaction_type", "tenure",
    "total_floor_area", "number_habitable_rooms", "number_heated_rooms",
    "inspection_date", "lodgement_date", "lodgement_datetime",
    "local_authority", "constituency", "county",
    "norm_postcode", "paon_number", "paon_name", "saon_token", "norm_street",
]


# --- normalisation (mirror of the PPD-side SQL in 03_epc_schema.sql) --------------------
_SAON_RE = re.compile(r"\b(?:FLAT|APARTMENT|APT|UNIT|ROOM|FLT)\.?\s*([0-9]+)", re.I)
_LEADING_FLAT_RE = re.compile(r"^\s*([0-9]+)\s*,")          # "3, 14 Acacia Ave" -> flat 3
_NUMBER_RE = re.compile(r"\b([0-9]+[A-Z]?)\b", re.I)         # building number token
_NONALNUM = re.compile(r"[^A-Z0-9 ]")
_WS = re.compile(r"\s+")


def _norm_pc(postcode: str | None) -> str | None:
    if not postcode:
        return None
    return re.sub(r"\s+", "", postcode).upper() or None


def _clean(s: str) -> str:
    """Upper, drop punctuation, collapse whitespace — matches the SQL strip (single-spaced)."""
    return _WS.sub(" ", _NONALNUM.sub("", s.upper())).strip()


def normalise_epc_address(address1, address2, address3, postcode):
    """Return (norm_postcode, paon_number, paon_name, saon_token, norm_street).

    EPC ADDRESS1 is usually 'number street' (e.g. '14 ACACIA AVENUE') or a flat
    ('Flat 3, 14 Acacia Avenue' / 'Flat 3' with the building on ADDRESS2) or a house
    name ('The Old Rectory'). We extract the same keys PPD exposes structurally.
    """
    a1 = (address1 or "").strip()
    a2 = (address2 or "").strip()
    norm_postcode = _norm_pc(postcode)

    saon_token = None
    house_line = a1

    # 1) Explicit flat/apartment keyword (in address1, else address2) => SAON.
    for src in (a1, a2):
        m = _SAON_RE.search(src)
        if m:
            saon_token = m.group(1)
            break
    if saon_token is not None:
        house_line = _SAON_RE.sub("", a1).strip(" ,")
        if not _NUMBER_RE.search(house_line):     # a1 was only the flat ('Flat 3')
            house_line = a2 or house_line
    else:
        # 2) Leading 'N,' is a flat ONLY if a building number follows it ('3, 14 Acacia Ave').
        #    Otherwise 'N, Street' is just house number N ('25, The Avenue') — leave it.
        m = _LEADING_FLAT_RE.match(a1)
        if m and _NUMBER_RE.search(a1[m.end():]):
            saon_token = m.group(1)
            house_line = a1[m.end():].strip(" ,")

    paon_number = paon_name = norm_street = None
    m = _NUMBER_RE.search(house_line)
    if m and house_line[:m.start()].strip(" ,") == "":
        # leading number => building number; the rest is the street
        paon_number = m.group(1).upper()
        norm_street = _clean(house_line[m.end():]) or _clean(a2) or None
    elif m:
        # number mid-line (e.g. 'Acacia Court 14') => building number; street is the rest
        paon_number = m.group(1).upper()
        norm_street = _clean(house_line[:m.start()] + " " + house_line[m.end():]) or None
    else:
        # no number => named property; PAON name is the first comma-segment, street the rest/ADDRESS2
        first, _, rest = house_line.partition(",")
        paon_name = _clean(first) or None
        norm_street = _clean(rest) or _clean(a2) or None

    return norm_postcode, paon_number, paon_name, saon_token, norm_street


# --- value coercion ---------------------------------------------------------------------
def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _i(v):
    f = _f(v)
    return int(f) if f is not None else None


def parse_date(v):
    if not v:
        return None
    try:
        return datetime.strptime(v.strip()[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _dt(v):
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(v.strip()[:19], fmt)
        except (TypeError, ValueError):
            continue
    return None


def _rating(v):
    v = (v or "").strip().upper()
    return v if v in ("A", "B", "C", "D", "E", "F", "G") else None


def parse_epc_row(row: dict) -> tuple | None:
    """Map a certificate row (header-keyed) to the COLS tuple, or None to skip.

    Handles both the current bulk download (lowercase headers, id = 'certificate_number')
    and the older API/opendatacommunities format (uppercase, id = 'LMK_KEY'). We lowercase
    every header so one code path covers both.
    """
    r = {(k or "").strip().lower(): v for k, v in row.items()}
    lmk = (r.get("lmk_key") or r.get("certificate_number") or "").strip()
    if not lmk:
        return None
    a1, a2, a3 = r.get("address1"), r.get("address2"), r.get("address3")
    postcode = (r.get("postcode") or "").strip().upper() or None
    norm_pc, paon_number, paon_name, saon_token, norm_street = normalise_epc_address(a1, a2, a3, postcode)
    return (
        lmk,
        _i(r.get("uprn")),
        (r.get("uprn_source") or "").strip() or None,
        (r.get("building_reference_number") or "").strip() or None,
        (a1 or "").strip() or None, (a2 or "").strip() or None, (a3 or "").strip() or None,
        postcode,
        _rating(r.get("current_energy_rating")),
        _i(r.get("current_energy_efficiency")),
        (r.get("property_type") or "").strip() or None,
        (r.get("built_form") or "").strip() or None,
        (r.get("transaction_type") or "").strip() or None,
        (r.get("tenure") or "").strip() or None,
        _f(r.get("total_floor_area")),
        _f(r.get("number_habitable_rooms")),
        _f(r.get("number_heated_rooms")),
        parse_date(r.get("inspection_date")),
        parse_date(r.get("lodgement_date")),
        _dt(r.get("lodgement_datetime")),
        (r.get("local_authority") or "").strip() or None,
        (r.get("constituency") or "").strip() or None,
        (r.get("county") or "").strip() or None,
        norm_pc, paon_number, paon_name, saon_token, norm_street,
    )
