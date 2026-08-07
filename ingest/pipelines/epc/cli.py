"""Click CLI for the EPC ingest pipeline."""
import asyncio

import click

from ..settings import EPC_DIR
from .ingestor import EpcIngestor
from .parsing import parse_date


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
            await ingestor.ingest_api_incremental(parse_date(since) if since else None)
        if not no_refresh:
            await ingestor.refresh_epc()

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
