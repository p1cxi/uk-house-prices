#!/usr/bin/env python3
"""Entrypoint shim for the OS Code-Point Open postcode ingest.

Preserves `python postcode_ingest.py` for the postcode-ingest compose profile.
Real code lives in pipelines.postcodes.
"""
from pipelines.postcodes import ingest_postcodes

if __name__ == "__main__":
    ingest_postcodes()
