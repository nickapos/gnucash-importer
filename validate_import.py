#!/usr/bin/env python3
"""
validate_import.py
===================
Validates previously-imported GnuCash transactions against their source
CSV rows, and lets you fix mismatches (most commonly: wrong post_date from
before the date-parsing bug was fixed in gnucash_importer.py).

Recognizes three kinds of records in .imported_transactions.json:
  1. Genuine imports (have tx_guid) -- verified against the live book.
  2. Skipped-and-marked-as-imported rows (skipped=True) -- reported as
     intentionally skipped, never flagged as a mismatch.
  3. Legacy imports from before tx_guid tracking existed -- flagged as
     unverifiable, since there is no reliable way to find the matching
     GnuCash transaction automatically.

Usage:
    python validate_import.py --gnucash-file="portfolio-sqlite.gnucash" --csv-file="transactions.csv"
    python validate_import.py --gnucash-file="portfolio-sqlite.gnucash" --csv-file="transactions.csv" --fix
"""

import argparse
import json
import os
import sys
import hashlib
import csv as csv_module

import piecash

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gnucash_importer import (
    BankFormatMapper,
    DUPLICATE_CHECK_FILE,
    parse_tx_date,
)


def _tx_hash(tx: dict) -> str:
    d = f"{tx['date']}|{tx['payee']}|{tx['amount']:.2f}"
    return hashlib.md5(d.encode("utf-8")).hexdigest()


def read_csv_rows(csv_file: str, skip_pending: bool = True):
    mapper = BankFormatMapper()
    fmt = mapper.detect_format(csv_file)
    print(f"Detected bank format: {fmt}")
    rows = []
    with open(csv_file, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv_module.DictReader(f)
        for i, row in enumerate(reader, 1):
            if not any(str(v).strip() for v in row.values()):
                continue
            if fmt == "revolut":
                m = mapper.map_revolut(row)
            elif fmt == "bof_scot":
                m = mapper.map_bof_scot(row)
            else:
                m = mapper.map_generic(row)
            if skip_pending and str(m.get("state", "")).upper() == "PENDING":
                continue
            m["row_number"] = i
            m["import_hash"] = _tx_hash(m)
            rows.append(m)
    return rows


def load_imported() -> dict:
    if os.path.exists(DUPLICATE_CHECK_FILE):
        with open(DUPLICATE_CHECK_FILE, "r") as f:
            return json.load(f)
    return {}


def main():
    parser = argparse.ArgumentParser(
        description="Validate/repair imported GnuCash transactions against their source CSV"
    )
    parser.add_argument("--gnucash-file", required=True, help="Path to .gnucash (sqlite) file")
    parser.add_argument("--csv-file", required=True, help="Path to the original bank CSV export")
    parser.add_argument(
        "--fix", action="store_true",
        help="Actually correct mismatched post_dates (default: report only)"
    )
    parser.add_argument(
        "--include-pending", action="store_true",
        help="Include PENDING rows in validation"
    )
    args = parser.parse_args()

    imported = load_imported()
    rows = read_csv_rows(args.csv_file, skip_pending=not args.include_pending)
    print(f"Read {len(rows)} CSV rows\n")

    book = piecash.open_book(
        os.path.abspath(args.gnucash_file), readonly=not args.fix, open_if_lock=True
    )

    checked = 0
    skipped_count = 0
    legacy_count = 0
    mismatches = []
    not_found = []

    try:
        for row in rows:
            record = imported.get(row["import_hash"])
            if not record:
                not_found.append(row)
                continue

            if record.get("skipped"):
                skipped_count += 1
                continue

            checked += 1
            tx_guid = record.get("tx_guid")
            expected_date = parse_tx_date(row["date"]).date()

            if not tx_guid:
                legacy_count += 1
                print(
                    f"Row {row['row_number']} ('{row['payee']}'): imported before "
                    f"tx_guid tracking existed - cannot verify automatically. "
                    f"Expected date: {expected_date}"
                )
                continue

            txn = next((t for t in book.transactions if t.guid == tx_guid), None)
            if txn is None:
                print(
                    f"Row {row['row_number']} ('{row['payee']}'): recorded tx_guid "
                    f"{tx_guid} not found in book (may have been deleted)."
                )
                continue

            actual_date = txn.post_date
            if actual_date != expected_date:
                mismatches.append((row, txn, expected_date, actual_date))
                print(
                    f"MISMATCH Row {row['row_number']} ('{row['payee']}'): "
                    f"book has {actual_date}, CSV says {expected_date}"
                )

        print(f"\nChecked {checked} previously-imported rows.")
        print(f"  {skipped_count} row(s) were intentionally skipped-and-marked-imported.")
        print(f"  {legacy_count} row(s) are legacy imports without tx_guid (unverifiable).")
        print(f"Found {len(mismatches)} date mismatch(es).")
        if not_found:
            print(
                f"{len(not_found)} CSV row(s) have no matching import record "
                f"(not yet imported, or hash changed)."
            )

        if mismatches and args.fix:
            print("\nFixing mismatched dates...")
            for row, txn, expected_date, actual_date in mismatches:
                txn.post_date = expected_date
                print(f"  Fixed '{row['payee']}': {actual_date} -> {expected_date}")
            book.save()
            print(f"\nSaved {len(mismatches)} correction(s) to {args.gnucash_file}")
        elif mismatches:
            print("\nRun again with --fix to correct these dates.")

    finally:
        book.close()


if __name__ == "__main__":
    main()
