"""Click CLI for the PPD ingest pipeline.

Preserves the original `python ingest.py --mode ...` invocation the systemd
timer and docker-compose services use; the shim at ingest/ingest.py just
delegates here.
"""
import asyncio
import logging
import os
import sys
from typing import Optional

import click
import structlog

from ..settings import DB_CONFIG
from .ingestor import LandRegistryIngestor

logger = structlog.get_logger(__name__)


@click.command()
@click.option("--mode",
              type=click.Choice(["full", "yearly", "monthly", "backfill", "postcode-lookup"]),
              required=True,
              help="Ingestion mode")
@click.option("--year", type=int, help="Year for yearly mode (e.g. 2023)")
@click.option("--force-bulk", is_flag=True,
              help="Force bulk COPY mode for yearly ingests even if data exists (may fail on conflicts)")
@click.option("--log-level",
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
              default="INFO",
              help="Logging level")
def main(mode: str, year: Optional[int], force_bulk: bool, log_level: str):
    """UK House Prices Data Ingestion Service."""
    logging.basicConfig(level=getattr(logging, log_level))
    logger.info("Starting ingestion service",
                mode=mode, year=year,
                target_counties=os.getenv("TARGET_COUNTIES", "").split(","),
                db_host=DB_CONFIG["host"])

    ingestor = LandRegistryIngestor()

    try:
        if mode == "full":
            asyncio.run(ingestor.ingest_full_dataset())
        elif mode == "yearly":
            if not year:
                click.echo("Error: --year is required for yearly mode", err=True)
                sys.exit(1)
            asyncio.run(ingestor.ingest_yearly_data(year, force_bulk=force_bulk))
        elif mode == "monthly":
            asyncio.run(ingestor.ingest_monthly_updates())
        elif mode == "backfill":
            asyncio.run(ingestor.ingest_backfill())
        elif mode == "postcode-lookup":
            click.echo("Postcode lookup ingestion not yet implemented", err=True)
            sys.exit(1)
    except Exception as e:
        logger.error("Ingestion failed", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
