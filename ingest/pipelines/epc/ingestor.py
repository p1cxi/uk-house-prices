"""EPC ingestor: bulk-file loader + developer-API incremental loader.

Two paths, both feeding `epc_certificates`:
  - bulk-local: walks a GOV.UK One Login download tree (per-LA folders each with
    a certificates.csv) and COPYs it in. This is the one-time full load.
  - api: incremental top-up via the developer API (HTTP Basic auth = email + API
    key), for quarterly refreshes.

After either path, `refresh_epc()` rebuilds the EPC matviews in dependency order.
"""
import csv
import os
from datetime import date

import httpx
import psycopg
import structlog

from ..settings import DB_CONFIG, EPC_API_BASE, EPC_API_EMAIL, EPC_API_KEY, EPC_DIR
from .parsing import COLS, parse_epc_row

logger = structlog.get_logger()


class EpcIngestor:
    def __init__(self):
        self.stats = {"files": 0, "read": 0, "inserted": 0, "skipped": 0}

    async def _connect(self):
        conn = await psycopg.AsyncConnection.connect(**DB_CONFIG)
        await conn.set_autocommit(True)
        return conn

    async def _copy_batch(self, conn, batch):
        cols = ", ".join(COLS)
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
        cols = ", ".join(COLS)
        ph = ", ".join(["%s"] * len(COLS))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLS if c != "lmk_key")
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
