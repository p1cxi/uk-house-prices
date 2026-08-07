"""OS Code-Point Open postcode loader.

Reads per-area CSVs from CSV_DIR, reprojects easting/northing (EPSG:27700) to
lat/lng (EPSG:4326), and upserts into the `postcodes` table. Synchronous — this
runs once as a compose profile job, not from the API.
"""
import csv
import os

import psycopg
from pyproj import Transformer

from ..settings import DB_CONFIG

CSV_DIR = os.getenv("POSTCODE_CSV_DIR", "./postcode_csv/CSV")

_transformer = Transformer.from_crs("EPSG:27700", "EPSG:4326")


def ingest_postcodes(csv_dir: str = CSV_DIR) -> int:
    """Load every *.csv in csv_dir into the postcodes table. Returns total upserted."""
    conn = psycopg.connect(**DB_CONFIG)
    cur = conn.cursor()
    total = 0

    try:
        for filename in sorted(os.listdir(csv_dir)):
            if not filename.endswith(".csv"):
                continue

            filepath = os.path.join(csv_dir, filename)
            batch = []
            with open(filepath, "r") as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) < 4:
                        continue
                    postcode = row[0].strip().upper()
                    try:
                        easting = float(row[2])
                        northing = float(row[3])
                        lat, lng = _transformer.transform(easting, northing)
                        batch.append((postcode, lat, lng))
                    except (ValueError, IndexError):
                        continue

            if batch:
                cur.executemany(
                    """
                    INSERT INTO postcodes (postcode, lat, lng)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (postcode) DO UPDATE SET
                        lat = EXCLUDED.lat,
                        lng = EXCLUDED.lng
                    """,
                    batch,
                )
                conn.commit()
                total += len(batch)
                print(f"{filename}: {len(batch)} postcodes inserted ({total} total)")

    finally:
        cur.close()
        conn.close()

    print(f"Done. {total} postcodes total.")
    return total
