#!/usr/bin/env python3
"""CLI entry point for the modular GnuCash CSV importer."""

import argparse
import os
import traceback

from config import (
    ACCOUNTS_EXPORT_FILE,
    DEFAULT_CSV_FILE,
    DEFAULT_GNUCASH_FILE,
    DEFAULT_KEEP_BACKUPS,
    DEFAULT_PENDING_DUPLICATE_WINDOW_DAYS,
    TRANSACTION_HISTORY_FILE,
)
from importer import TransactionImporter
from mappings import PayeeAccountMapper, TransactionHistoryAnalyzer


def main() -> None:
    parser = argparse.ArgumentParser(description="Import bank CSV into a SQLite GnuCash book")
    parser.add_argument("--gnucash-file", default=DEFAULT_GNUCASH_FILE)
    parser.add_argument("--csv-file", default=DEFAULT_CSV_FILE)
    parser.add_argument("--source-account", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--auto-accept", action="store_true")
    parser.add_argument("--include-pending", action="store_true", help="Include pending Revolut transactions")
    parser.add_argument(
        "--pending-duplicate-window-days",
        type=int,
        default=DEFAULT_PENDING_DUPLICATE_WINDOW_DAYS,
        help="Date window used to match pending rows to completed ledger entries (default: 7)",
    )
    parser.add_argument("--no-ledger-check", action="store_true")
    parser.add_argument("--keep-backups", type=int, default=DEFAULT_KEEP_BACKUPS)
    parser.add_argument("--list-mappings", action="store_true")
    parser.add_argument("--clear-mappings", action="store_true")
    parser.add_argument("--clear-history", action="store_true")
    parser.add_argument("--export-accounts", action="store_true")
    args = parser.parse_args()

    mapper = PayeeAccountMapper()
    if args.list_mappings:
        for payee, data in mapper.list_mappings():
            print(f"{payee!r} -> {data['account_fullname']}")
        return
    if args.clear_mappings:
        if input("Delete ALL mappings? [y/N]: ").strip().lower() == "y":
            mapper.mappings = {}
            mapper.save()
        return
    if args.clear_history:
        if os.path.exists(TRANSACTION_HISTORY_FILE):
            os.remove(TRANSACTION_HISTORY_FILE)
            print("History cache cleared")
        return

    importer = None
    try:
        importer = TransactionImporter(args.gnucash_file, args.csv_file)
        importer.dry_run = args.dry_run
        importer.auto_accept = args.auto_accept
        importer.include_pending = args.include_pending
        importer.pending_duplicate_window_days = max(0, args.pending_duplicate_window_days)
        importer.keep_backups = max(0, args.keep_backups)
        importer.check_ledger = not args.no_ledger_check
        importer.payee_mapper = mapper
        importer.open_book(readonly=args.dry_run)

        if args.export_accounts:
            importer.export_accounts_json(ACCOUNTS_EXPORT_FILE)
            return

        importer.history_analyzer = TransactionHistoryAnalyzer(importer.book)
        importer.history_analyzer.analyze()
        from account_matching import AccountMatcher
        importer.matcher = AccountMatcher(importer.book, mapper, importer.history_analyzer)
        importer.source_account = importer.resolve_source_account(args.source_account)
        rows = importer.read_csv()
        importer.process_transactions(rows)

        if importer.tx_to_create and not args.dry_run:
            if args.auto_accept or input("Execute import now? [y/N]: ").strip().lower() == "y":
                importer.execute_import()
        elif args.dry_run:
            print("Dry run complete: no accounts, skipped records, mappings, backups, or transactions were written.")
    except KeyboardInterrupt:
        print("Cancelled by user")
    except Exception as exc:
        print(f"Error: {exc}")
        traceback.print_exc()
    finally:
        if importer and importer.book:
            try:
                importer.close_book()
            except Exception as exc:
                print(f"Warning: could not cleanly close book: {exc}")


if __name__ == "__main__":
    main()
