"""MHCLG domestic EPC (Energy Performance Certificate) ingestion.

`EpcIngestor` loads certificates from either the one-time bulk download or
the developer API (quarterly top-ups) and refreshes the EPC materialized views.
"""
from .ingestor import EpcIngestor

__all__ = ["EpcIngestor"]
