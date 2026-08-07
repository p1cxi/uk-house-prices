#!/usr/bin/env python3
"""Entrypoint shim for the PPD (HM Land Registry) ingest CLI.

Kept at the container root so the historical `python ingest.py --mode monthly`
invocation used by the systemd timer and docker-compose services still works.
Real code lives in pipelines.ppd.
"""
from pipelines.ppd.cli import main

if __name__ == "__main__":
    main()
