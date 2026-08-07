"""CSV parsing and validation for HM Land Registry PPD rows.

Parses the 16-column Land Registry format, coerces types, and optionally
filters to a set of target counties (env var TARGET_COUNTIES).
"""
import csv
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple


class ParseResult(Enum):
    SUCCESS = "success"
    INVALID_FORMAT = "invalid_format"
    PARSE_ERROR = "parse_error"
    FILTERED_OUT = "filtered_out"


def parse_csv_line(line: str) -> List[str]:
    """Parse a single CSV line, handling quoted fields."""
    csv_reader = csv.reader([line])
    try:
        return next(csv_reader)
    except StopIteration:
        return []


def parse_transaction_row(row: List[str]) -> Tuple[ParseResult, Optional[Dict], Optional[str]]:
    """Parse a single 16-field transaction row. Returns (result, transaction_dict, error)."""
    if len(row) != 16:
        return ParseResult.INVALID_FORMAT, None, f"Expected 16 fields, got {len(row)}"

    try:
        date_str = row[2].strip()
        transaction_date = datetime.strptime(date_str, "%Y-%m-%d %H:%M").date() if date_str else None

        try:
            price = int(row[1]) if row[1].strip() else 0
        except ValueError:
            price = 0

        transaction = {
            "transaction_id": row[0].strip(),
            "price": price,
            "date": transaction_date,
            "postcode": row[3].strip().upper() if row[3] else None,
            "property_type": row[4].strip().upper() if row[4] else None,
            "new_build": row[5].strip().upper() if row[5] else None,
            "tenure": row[6].strip().upper() if row[6] else None,
            "paon": row[7].strip() if row[7] else None,
            "saon": row[8].strip() if row[8] else None,
            "street": row[9].strip() if row[9] else None,
            "locality": row[10].strip() if row[10] else None,
            "town": row[11].strip() if row[11] else None,
            "district": row[12].strip() if row[12] else None,
            "county": row[13].strip().upper() if row[13] else None,
            "ppd_type": row[14].strip().upper() if row[14] else None,
            "record_status": row[15].strip().upper() if row[15] else "A",
        }
        return ParseResult.SUCCESS, transaction, None

    except Exception as e:
        return ParseResult.PARSE_ERROR, None, str(e)


def should_include_transaction(transaction: Dict, target_counties: Optional[List[str]] = None) -> bool:
    """Include if no filter set, or the row's county is in the target list."""
    if not target_counties:
        return True
    county = transaction.get("county")
    if not county:
        return False
    return county in target_counties
