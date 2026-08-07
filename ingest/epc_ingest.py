#!/usr/bin/env python3
"""Entrypoint shim for the EPC ingest CLI.

Preserves `python epc_ingest.py --mode bulk` for the epc-ingest compose profile.
Real code lives in pipelines.epc.
"""
from pipelines.epc.cli import cli

if __name__ == "__main__":
    cli()
