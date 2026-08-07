"""HM Land Registry PPD ingestor: stream, parse, and load into `transactions`.

Two write modes:
  - bulk COPY (fast; used for full/yearly loads when the year has no existing rows)
  - per-row upsert (handles A/C/D record status; used for monthly deltas and re-ingests)
"""
import calendar
from datetime import date, timedelta
from typing import Dict, List, Optional

import httpx
import psycopg
import structlog
from psycopg.rows import dict_row
from tqdm import tqdm

from ..errors import DatabaseError, DownloadError
from ..settings import DB_CONFIG, LAND_REGISTRY_URLS
from .parsing import ParseResult, parse_csv_line, parse_transaction_row, should_include_transaction

logger = structlog.get_logger(__name__)


class LandRegistryIngestor:
    """Downloads, parses and writes HM Land Registry Price Paid Data."""

    def __init__(self):
        self.db_config = DB_CONFIG
        self.session_stats = {
            "downloaded_rows": 0,
            "processed_rows": 0,
            "inserted_rows": 0,
            "updated_rows": 0,
            "deleted_rows": 0,
            "filtered_rows": 0,
            "error_rows": 0,
        }

    async def get_database_connection(self) -> psycopg.AsyncConnection:
        try:
            conn = await psycopg.AsyncConnection.connect(**self.db_config)
            await conn.set_autocommit(True)
            return conn
        except Exception as e:
            logger.error("Failed to connect to database", error=str(e), config=self.db_config)
            raise DatabaseError(f"Database connection failed: {e}")

    async def stream_csv_data(
        self,
        conn: psycopg.AsyncConnection,
        url: str,
        batch_size: int = 1000,
        bulk_mode: bool = False,
    ):
        """Stream a Land Registry CSV over HTTP and load it in batches."""
        logger.info("Starting streaming download and processing",
                    url=url, batch_size=batch_size, bulk_mode=bulk_mode)
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("GET", url, follow_redirects=True) as response:
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    total_mb = round(int(content_length) / 1024 / 1024, 2) if content_length else None
                    logger.info("Starting stream processing", url=url, size_mb=total_mb,
                                mode="COPY bulk" if bulk_mode else "upsert delta")
                    await self._process_streaming_csv(conn, response, batch_size, total_mb, bulk_mode)
        except httpx.RequestError as e:
            logger.error("Download failed", url=url, error=str(e))
            raise DownloadError(f"Failed to download {url}: {e}")
        except httpx.HTTPStatusError as e:
            logger.error("HTTP error during download", url=url, status=e.response.status_code)
            raise DownloadError(f"HTTP {e.response.status_code} for {url}")

    async def _process_streaming_csv(
        self,
        conn: psycopg.AsyncConnection,
        response,
        batch_size: int,
        total_mb: Optional[float],
        bulk_mode: bool = False,
    ):
        buffer = ""
        batch: List[Dict] = []
        bytes_processed = 0

        with tqdm(desc="Processing transactions", unit="rows",
                  postfix={"MB processed": 0, "inserted": 0, "filtered": 0}) as pbar:

            async for chunk in response.aiter_bytes(chunk_size=65536):
                bytes_processed += len(chunk)
                buffer += chunk.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    self.session_stats["downloaded_rows"] += 1
                    pbar.update(1)

                    try:
                        row = parse_csv_line(line)
                        result, transaction, error_msg = parse_transaction_row(row)

                        if result != ParseResult.SUCCESS:
                            if error_msg:
                                logger.warning("Failed to parse CSV line", error=error_msg, line=line[:100])
                            self.session_stats["error_rows"] += 1
                            continue

                        if transaction is None:
                            self.session_stats["error_rows"] += 1
                            continue
                        if not should_include_transaction(transaction):
                            self.session_stats["filtered_rows"] += 1
                            continue

                        batch.append(transaction)
                        self.session_stats["processed_rows"] += 1

                        if len(batch) >= batch_size:
                            await self._dispatch_batch(conn, batch, bulk_mode)
                            batch = []
                            mb_processed = round(bytes_processed / 1024 / 1024, 1)
                            pbar.set_postfix({
                                "MB processed": mb_processed,
                                "inserted": self.session_stats["inserted_rows"],
                                "updated": self.session_stats["updated_rows"],
                                "filtered": self.session_stats["filtered_rows"],
                            })

                    except Exception as e:
                        logger.warning("Failed to process CSV line", error=str(e), line=line[:100])
                        self.session_stats["error_rows"] += 1

            # Final partial line (no trailing newline).
            if buffer.strip():
                try:
                    row = parse_csv_line(buffer.strip())
                    result, transaction, error_msg = parse_transaction_row(row)
                    if result == ParseResult.SUCCESS:
                        if transaction is None:
                            self.session_stats["error_rows"] += 1
                        elif should_include_transaction(transaction):
                            batch.append(transaction)
                            self.session_stats["processed_rows"] += 1
                        else:
                            self.session_stats["filtered_rows"] += 1
                    else:
                        if error_msg:
                            logger.warning("Failed to process final buffer", error=error_msg)
                        self.session_stats["error_rows"] += 1
                except Exception as e:
                    logger.warning("Failed to process final buffer", error=str(e))
                    self.session_stats["error_rows"] += 1

            if batch:
                await self._dispatch_batch(conn, batch, bulk_mode)

        logger.info("Streaming processing complete",
                    bytes_processed=bytes_processed,
                    mb_processed=round(bytes_processed / 1024 / 1024, 2),
                    stats=self.session_stats)

    async def upsert_transaction(self, conn: psycopg.AsyncConnection, transaction: Dict):
        """Insert-or-update one transaction; handles record_status='D' as a delete."""
        async with conn.cursor() as cursor:
            if transaction["record_status"] == "D":
                await cursor.execute(
                    "DELETE FROM transactions WHERE transaction_id = %s",
                    (transaction["transaction_id"],),
                )
                self.session_stats["deleted_rows"] += 1
            else:
                insert_sql = """
                INSERT INTO transactions (
                    transaction_id, price, date, postcode, property_type,
                    new_build, tenure, paon, saon, street, locality,
                    town, district, county, ppd_type, record_status
                ) VALUES (
                    %(transaction_id)s, %(price)s, %(date)s, %(postcode)s, %(property_type)s,
                    %(new_build)s, %(tenure)s, %(paon)s, %(saon)s, %(street)s, %(locality)s,
                    %(town)s, %(district)s, %(county)s, %(ppd_type)s, %(record_status)s
                )
                ON CONFLICT (transaction_id) DO UPDATE SET
                    price = EXCLUDED.price,
                    date = EXCLUDED.date,
                    postcode = EXCLUDED.postcode,
                    property_type = EXCLUDED.property_type,
                    new_build = EXCLUDED.new_build,
                    tenure = EXCLUDED.tenure,
                    paon = EXCLUDED.paon,
                    saon = EXCLUDED.saon,
                    street = EXCLUDED.street,
                    locality = EXCLUDED.locality,
                    town = EXCLUDED.town,
                    district = EXCLUDED.district,
                    county = EXCLUDED.county,
                    ppd_type = EXCLUDED.ppd_type,
                    record_status = EXCLUDED.record_status,
                    ingested_at = now()
                """
                await cursor.execute(insert_sql, transaction)
                if cursor.rowcount == 1:
                    self.session_stats["inserted_rows"] += 1
                else:
                    self.session_stats["updated_rows"] += 1

    async def process_batch_bulk(self, conn: psycopg.AsyncConnection, batch: List[Dict]):
        """Bulk COPY path — fastest, but only safe when there are no ON CONFLICT rows."""
        if not batch:
            return
        try:
            async with conn.cursor() as cursor:
                async with cursor.copy(
                    "COPY transactions ("
                    "transaction_id, price, date, postcode, property_type, new_build, tenure, "
                    "paon, saon, street, locality, town, district, county, ppd_type, record_status"
                    ") FROM STDIN"
                ) as copy:
                    for t in batch:
                        await copy.write_row((
                            t["transaction_id"], t["price"], t["date"], t["postcode"], t["property_type"],
                            t["new_build"], t["tenure"], t["paon"], t["saon"], t["street"], t["locality"],
                            t["town"], t["district"], t["county"], t["ppd_type"], t["record_status"],
                        ))
            self.session_stats["inserted_rows"] += len(batch)
            logger.debug("Bulk inserted batch", count=len(batch))
        except Exception as e:
            logger.error("Failed to bulk insert batch", batch_size=len(batch), error=str(e))
            logger.info("Falling back to individual upserts for failed batch")
            await self.process_batch(conn, batch)

    async def process_batch(self, conn: psycopg.AsyncConnection, batch: List[Dict]):
        """Per-row upsert path — handles monthly A/C/D deltas idempotently."""
        for transaction in batch:
            try:
                await self.upsert_transaction(conn, transaction)
            except Exception as e:
                logger.error("Failed to upsert transaction",
                             transaction_id=transaction.get("transaction_id"), error=str(e))
                self.session_stats["error_rows"] += 1

    async def _dispatch_batch(self, conn: psycopg.AsyncConnection, batch: List[Dict], bulk_mode: bool):
        if bulk_mode:
            await self.process_batch_bulk(conn, batch)
        else:
            await self.process_batch(conn, batch)

    async def refresh_materialized_views(self, conn: psycopg.AsyncConnection):
        logger.info("Refreshing materialized views")
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT refresh_monthly_stats()")
        logger.info("Materialized views refreshed")

    async def get_data_freshness(self, conn: psycopg.AsyncConnection) -> Dict:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute("SELECT * FROM get_data_freshness()")
            result = await cursor.fetchone()
            return dict(result) if result else {}

    async def ingest_full_dataset(self):
        logger.info("Starting full dataset ingestion with bulk COPY")
        conn = await self.get_database_connection()
        try:
            await self.stream_csv_data(conn, LAND_REGISTRY_URLS["full"], batch_size=5000, bulk_mode=True)
            await self.refresh_materialized_views(conn)
            freshness = await self.get_data_freshness(conn)
            logger.info("Full ingestion complete", stats=self.session_stats, freshness=freshness)
        finally:
            await conn.close()

    async def check_year_has_data(self, conn: psycopg.AsyncConnection, year: int) -> bool:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT COUNT(*) FROM transactions WHERE EXTRACT(YEAR FROM date) = %s LIMIT 1",
                (year,),
            )
            result = await cursor.fetchone()
            return result[0] > 0 if result else False

    async def ingest_yearly_data(self, year: int, force_bulk: bool = False):
        """Ingest one year; picks bulk vs upsert based on whether the year already has rows."""
        conn = await self.get_database_connection()
        try:
            has_existing_data = await self.check_year_has_data(conn, year)
            if has_existing_data and not force_bulk:
                logger.info("Found existing data for year, using upsert mode to avoid COPY conflicts",
                            year=year)
                bulk_mode = False
                batch_size = 1000
                mode_desc = "upsert mode (existing data detected)"
            else:
                if force_bulk and has_existing_data:
                    logger.warning("Force bulk mode enabled despite existing data - COPY may fail on conflicts",
                                   year=year)
                logger.info("Using bulk COPY mode for maximum performance", year=year)
                bulk_mode = True
                batch_size = 5000
                mode_desc = "bulk COPY mode"

            logger.info("Starting yearly data ingestion", year=year, mode=mode_desc)
            url = LAND_REGISTRY_URLS["yearly"].format(year=year)
            await self.stream_csv_data(conn, url, batch_size=batch_size, bulk_mode=bulk_mode)
            await self.refresh_materialized_views(conn)
            freshness = await self.get_data_freshness(conn)
            logger.info("Yearly ingestion complete", year=year, mode=mode_desc,
                        stats=self.session_stats, freshness=freshness)
        finally:
            await conn.close()

    async def ingest_monthly_updates(self):
        logger.info("Starting monthly updates ingestion with individual upserts for A/C/D handling")
        conn = await self.get_database_connection()
        try:
            await self.stream_csv_data(conn, LAND_REGISTRY_URLS["monthly"], batch_size=500, bulk_mode=False)
            await self.refresh_materialized_views(conn)
            freshness = await self.get_data_freshness(conn)
            logger.info("Monthly updates complete", stats=self.session_stats, freshness=freshness)
        finally:
            await conn.close()

    async def get_covered_months(self, conn: psycopg.AsyncConnection) -> List[date]:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT DISTINCT DATE_TRUNC('month', date)::date AS month
                FROM transactions
                WHERE ppd_type = 'A' AND record_status = 'A'
                ORDER BY month
                """
            )
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def detect_missing_months(self, conn: psycopg.AsyncConnection) -> List[date]:
        """Months from earliest data up to (but excluding) the current month that have no rows."""
        covered = set(await self.get_covered_months(conn))
        if not covered:
            return []
        start = min(covered)
        end = date.today().replace(day=1)  # exclusive: current month may be incomplete
        missing = []
        cur = start
        while cur < end:
            if cur not in covered:
                missing.append(cur)
            _, days_in_month = calendar.monthrange(cur.year, cur.month)
            cur = (cur + timedelta(days=days_in_month)).replace(day=1)
        return missing

    async def ingest_backfill(self):
        """Detect missing months and re-ingest the affected years."""
        conn = await self.get_database_connection()
        try:
            missing = await self.detect_missing_months(conn)
            if not missing:
                logger.info("No missing months detected — nothing to backfill")
                return
            years_to_backfill = sorted({m.year for m in missing})
            logger.info("Missing months detected", count=len(missing), years=years_to_backfill,
                        months=[m.isoformat() for m in missing])
            for year in years_to_backfill:
                logger.info("Backfilling year", year=year)
                await self.ingest_yearly_data(year)
            freshness = await self.get_data_freshness(conn)
            logger.info("Backfill complete", years=years_to_backfill, freshness=freshness)
        finally:
            await conn.close()
