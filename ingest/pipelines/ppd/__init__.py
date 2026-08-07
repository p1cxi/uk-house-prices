"""HM Land Registry Price Paid Data ingestion.

Handles the full historical dataset, per-year backfills, and monthly deltas.
`LandRegistryIngestor` streams the public CSVs directly into Postgres.
"""
from .ingestor import LandRegistryIngestor

__all__ = ["LandRegistryIngestor"]
