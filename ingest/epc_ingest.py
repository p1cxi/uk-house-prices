"""Ingest MHCLG domestic EPC data (England & Wales, OGL v3.0) into epc_certificates.

Two paths:
  - bulk-local: walk a GOV.UK One Login bulk download (per-LA folders each with a
    certificates.csv) and COPY it in. This is the one-time full load. Mirrors
    postcode_ingest.py's local-file model; reuses LandRegistryIngestor's COPY pattern.
  - api: incremental top-up via the developer API (HTTP Basic auth = email + API key),
    for quarterly refreshes. Optional.

Address NORMALISATION (norm_postcode / paon_number / paon_name / saon_token / norm_street)
happens here, once, and MUST mirror the PPD-side SQL in postgres/init/03_epc_schema.sql
(transaction_epc) so the two sides join. Land Registry PPD has clean paon/saon/street
columns; EPC ships free-text ADDRESS1/2/3, so we parse them into the same shape.

Run (initial full load, via the epc-ingest compose profile):
  docker compose run --rm epc-ingest python epc_ingest.py --mode bulk
Incremental (needs EPC_API_EMAIL / EPC_API_KEY in env):
  docker compose run --rm epc-ingest python epc_ingest.py --mode api --since 2026-01-01
"""
import asyncio
import csv
import os
import re
from datetime import date, datetime

import click
import httpx
import psycopg
import structlog

logger = structlog.get_logger()

DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": int(os.getenv("POSTGRES_PORT", 5432)),
    "dbname": os.getenv("POSTGRES_DB", "house_prices"),
    "user": os.getenv("POSTGRES_USER", "prices"),
    "password": os.getenv("POSTGRES_PASSWORD"),
}

EPC_DIR = os.getenv("EPC_DIR", "./epc_data")
EPC_API_BASE = os.getenv("EPC_API_BASE", "https://epc.opendatacommunities.org/api/v1/domestic/search")
EPC_API_EMAIL = os.getenv("EPC_API_EMAIL")
EPC_API_KEY = os.getenv("EPC_API_KEY")

# Columns we persist (order matches the COPY / upsert column list below).
_COLS = [
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


def _d(v):
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
    """Map a certificate row (header-keyed) to the _COLS tuple, or None to skip.

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
        _d(r.get("inspection_date")),
        _d(r.get("lodgement_date")),
        _dt(r.get("lodgement_datetime")),
        (r.get("local_authority") or "").strip() or None,
        (r.get("constituency") or "").strip() or None,
        (r.get("county") or "").strip() or None,
        norm_pc, paon_number, paon_name, saon_token, norm_street,
    )


class EpcIngestor:
    def __init__(self):
        self.stats = {"files": 0, "read": 0, "inserted": 0, "skipped": 0}

    async def _connect(self):
        conn = await psycopg.AsyncConnection.connect(**DB_CONFIG)
        await conn.set_autocommit(True)
        return conn

    async def _copy_batch(self, conn, batch):
        cols = ", ".join(_COLS)
        try:
            async with conn.cursor() as cur:
                async with cur.copy(f"COPY epc_certificates ({cols}) FROM STDIN") as copy:
                    for r in batch:
                        await copy.write_row(r)
            self.stats["inserted"] += len(batch)
        except Exception as e:
            # COPY fails wholesale on a duplicate lmk_key (re-run / overlapping LA files);
            # fall back to per-row upsert so re-ingest is idempotent.
            logger.warning("COPY batch failed, falling back to upsert", error=str(e).splitlines()[0])
            await self._upsert_batch(conn, batch)

    async def _upsert_batch(self, conn, batch):
        cols = ", ".join(_COLS)
        ph = ", ".join(["%s"] * len(_COLS))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _COLS if c != "lmk_key")
        sql = (f"INSERT INTO epc_certificates ({cols}) VALUES ({ph}) "
               f"ON CONFLICT (lmk_key) DO UPDATE SET {updates}, ingested_at = now()")
        async with conn.cursor() as cur:
            await cur.executemany(sql, batch)
        self.stats["inserted"] += len(batch)

    async def ingest_bulk_local(self, root_dir=EPC_DIR, batch_size=5000):
        """Walk root_dir for every certificates.csv and COPY it in."""
        conn = await self._connect()
        try:
            csv_paths = []
            for dirpath, _, files in os.walk(root_dir):
                for f in files:
                    fl = f.lower()
                    # 'certificates.csv' (per-LA layout) OR 'certificates-YYYY.csv' (per-year bulk).
                    # NEVER 'recommendations-*.csv' — different schema, many rows per certificate.
                    if fl.endswith(".csv") and fl.startswith("certificates"):
                        csv_paths.append(os.path.join(dirpath, f))
            logger.info("epc bulk load: found certificate files", count=len(csv_paths), root=root_dir)
            for path in sorted(csv_paths):
                await self._ingest_file(conn, path, batch_size)
                self.stats["files"] += 1
            logger.info("epc bulk load done", **self.stats)
        finally:
            await conn.close()

    async def _ingest_file(self, conn, path, batch_size):
        batch = []
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                self.stats["read"] += 1
                parsed = parse_epc_row(row)
                if parsed is None:
                    self.stats["skipped"] += 1
                    continue
                batch.append(parsed)
                if len(batch) >= batch_size:
                    await self._copy_batch(conn, batch)
                    batch = []
        if batch:
            await self._copy_batch(conn, batch)
        logger.info("ingested file", file=os.path.basename(os.path.dirname(path)) or path,
                    inserted=self.stats["inserted"])

    async def ingest_api_incremental(self, since: date | None = None, batch_size=500):
        """Page the developer API (Basic auth) and upsert. Optional quarterly top-up."""
        if not (EPC_API_EMAIL and EPC_API_KEY):
            raise RuntimeError("EPC_API_EMAIL and EPC_API_KEY must be set for --mode api")
        conn = await self._connect()
        auth = (EPC_API_EMAIL, EPC_API_KEY)
        headers = {"Accept": "text/csv"}
        params = {"size": str(batch_size)}
        if since:
            params["from-month"] = since.strftime("%Y-%m")
        search_after = None
        try:
            async with httpx.AsyncClient(timeout=120.0, auth=auth, headers=headers) as client:
                while True:
                    q = dict(params)
                    if search_after:
                        q["search-after"] = search_after
                    resp = await client.get(EPC_API_BASE, params=q)
                    if resp.status_code == 404 or not resp.text.strip():
                        break
                    resp.raise_for_status()
                    search_after = resp.headers.get("X-Next-Search-After")
                    rows = list(csv.DictReader(resp.text.splitlines()))
                    if not rows:
                        break
                    batch = [p for p in (parse_epc_row(r) for r in rows) if p]
                    self.stats["read"] += len(rows)
                    if batch:
                        await self._upsert_batch(conn, batch)
                    if not search_after:
                        break
            logger.info("epc api incremental done", **self.stats)
        finally:
            await conn.close()

    async def refresh_epc(self):
        """Rebuild the EPC matviews in dependency order (epc_property -> match -> coverage)."""
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                logger.info("refreshing EPC materialized views (this scans transactions)")
                await cur.execute("SELECT refresh_epc_stats()")
            logger.info("EPC matviews refreshed")
        finally:
            await conn.close()


@click.command()
@click.option("--mode", type=click.Choice(["bulk", "api"]), required=True)
@click.option("--root", default=EPC_DIR, help="bulk: directory of the One Login download")
@click.option("--since", default=None, help="api: YYYY-MM-DD lower bound for incremental fetch")
@click.option("--no-refresh", is_flag=True, help="skip the matview refresh after loading")
def cli(mode, root, since, no_refresh):
    """EPC ingestion CLI."""
    ingestor = EpcIngestor()

    async def _run():
        if mode == "bulk":
            await ingestor.ingest_bulk_local(root)
        else:
            await ingestor.ingest_api_incremental(_d(since) if since else None)
        if not no_refresh:
            await ingestor.refresh_epc()

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
