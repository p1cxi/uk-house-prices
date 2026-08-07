"""Data ingestion pipelines: PPD (Land Registry), EPC, and postcodes.

Shared setup (DB config, error types, structlog) lives in this package's
top-level modules; each sub-package (ppd/, epc/, postcodes/) implements one
data source with an Ingestor class and a click CLI.

The api container imports the ingestor classes directly:
    from pipelines.ppd import LandRegistryIngestor
    from pipelines.epc import EpcIngestor

The ingest container runs the CLIs via the top-level shim scripts
(ingest.py, epc_ingest.py, postcode_ingest.py) so systemd/compose invocations
stay unchanged.
"""
