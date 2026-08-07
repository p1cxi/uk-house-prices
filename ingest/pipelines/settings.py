"""Shared configuration for all ingest pipelines (DB + source URLs + EPC creds).

Also configures structlog on import so every pipeline emits structured JSON logs.
"""
import os

import structlog
from dotenv import load_dotenv

load_dotenv()


DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": int(os.getenv("POSTGRES_PORT", 5432)),
    "dbname": os.getenv("POSTGRES_DB", "house_prices"),
    "user": os.getenv("POSTGRES_USER", "prices"),
    "password": os.getenv("POSTGRES_PASSWORD"),
}


# HM Land Registry Price Paid Data feeds (public, no auth).
LAND_REGISTRY_URLS = {
    "full": "https://price-paid-data.publicdata.landregistry.gov.uk/pp-complete.csv",
    "yearly": "https://price-paid-data.publicdata.landregistry.gov.uk/pp-{year}.csv",
    "monthly": "https://price-paid-data.publicdata.landregistry.gov.uk/pp-monthly-update-new-version.csv",
}


# Optional geographic filter for target_counties (comma-separated env var, uppercased).
# None => no filter (all transactions ingested). Applied by pipelines.ppd.parsing.
def target_counties() -> list[str] | None:
    raw = os.getenv("TARGET_COUNTIES")
    if not raw:
        return None
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


# EPC (Energy Performance Certificate) data. Initial full load is a one-time manual
# bulk download (GOV.UK One Login) into EPC_DIR; quarterly top-ups use the developer
# API with HTTP Basic auth (email + API key) — kept ONLY in gitignored .env.
EPC_DIR = os.getenv("EPC_DIR", "./epc_data")
EPC_API_BASE = os.getenv(
    "EPC_API_BASE",
    "https://epc.opendatacommunities.org/api/v1/domestic/search",
)
EPC_API_EMAIL = os.getenv("EPC_API_EMAIL")
EPC_API_KEY = os.getenv("EPC_API_KEY")


structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
