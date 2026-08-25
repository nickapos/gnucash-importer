# GnuCash Bank Importer

A modular Python importer for posting bank CSV exports into a SQLite-backed GnuCash book.

The project supports Revolut, Bank of Scotland, Alpha Bank Greece, generic CSV exports, persistent payee mappings, balanced double-entry transactions, backups, source-account selection, duplicate detection, and pending-transaction reconciliation.

## Project Layout

The importer has been split into small modules:

| File | Responsibility |
|---|---|
| `main.py` | CLI entry point and orchestration |
| `config.py` | Shared defaults and bank/account constants |
| `utils.py` | Date parsing, terminal completion, payee normalization, account filters |
| `bank_formats.py` | Bank CSV detection and row mappers |
| `mappings.py` | Persistent payee mappings and history analysis |
| `account_matching.py` | Account caching and destination suggestions |
| `importer.py` | Core GnuCash writes, backups, duplicate checks, pending reconciliation |
| `income_expense_report.py` | Read-only income/expense/cash-flow reports |
| `validate_import.py` | Import validation and date repair helper |
| `clear_gnclock.py` | Stale SQLite GnuCash lock cleanup helper |

Run the importer using the new entry point:

```bash
python ./main.py --gnucash-file="portfolio-sqlite.gnucash" --csv-file="transactions.csv" --dry-run
```

## Requirements

- Python 3.9+
- SQLite-backed GnuCash book
- `piecash`
- SQLAlchemy compatible with your piecash release

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install piecash "SQLAlchemy<2"
```

For chart generation:

```bash
pip install matplotlib
```

## Basic Import

Review before any write:

```bash
python ./main.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --dry-run
```

Interactive import:

```bash
python ./main.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv"
```

Specify a source account directly:

```bash
python ./main.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --source-account="Assets:Current Assets:Revolut GBP"
```

## Pending Transactions

Revolut pending rows are ignored by default. Include them explicitly:

```bash
python ./main.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --include-pending
```

A pending row often later appears as a completed row with a different date, so its importer hash changes. The importer therefore performs a separate pending-to-completed duplicate check using:

- Selected source account
- Exact signed source-account amount
- Normalized payee similarity
- Date proximity window

Default date window: 7 days.

```bash
python ./main.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --include-pending \
  --pending-duplicate-window-days=7
```

When a likely completed counterpart is found, the importer displays both entries and asks whether the pending row is the same transaction. Confirming marks the pending row as skipped/imported in `.imported_transactions.json`, so it does not return in future imports.

## Backups

Before the first write in a non-dry-run session, the importer creates one timestamped copy of the SQLite book in:

```text
backups/
```

Example:

```text
backups/portfolio-sqlite.backup-20260825-041700.gnucash
```

Default retention is 10 backups:

```bash
--keep-backups=10
```

Keep 30:

```bash
--keep-backups=30
```

Keep every backup:

```bash
--keep-backups=0
```

## Supported Bank Formats

### Revolut

```text
Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
```

### Bank of Scotland

```text
Date,Description,Type,Money In (£),Money Out (£),Balance (£)
```

Download a CSV from online banking; PDF statements are not directly importable.

### Alpha Bank Greece

Uses semicolon-delimited Greek headings, a metadata preamble, European number formatting, and debit/credit markers:

```text
Α/Α;Ημ/νία;Αιτιολογία;Κατάστημα;Τοκισμός από;Αρ. συναλλαγής;Ποσό;Πρόσημο ποσού
```

- `Χ` means debit/negative.
- `Π` means credit/positive.
- Currency is treated as EUR.

## Duplicate Checks

The importer checks:

1. `.imported_transactions.json` — rows previously imported or permanently skipped by the tool.
2. Existing transactions in the selected GnuCash source account, using date and signed amount.
3. Pending-to-completed reconciliation when `--include-pending` is enabled.

Disable the existing-ledger check only when you know the CSV is completely new:

```bash
--no-ledger-check
```

## Skipping

The interactive account selector supports:

```text
Skip and mark as imported (never ask again)
Skip for now (ask again next run)
```

A permanent skip creates no GnuCash transaction. It writes a `skipped: true` record into `.imported_transactions.json`.

## Reports

`income_expense_report.py` is read-only and can create native-currency or consolidated reports.

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026
```

See the report generator’s `--help` output for currency conversion, detail, CSV, and plot options.

## Locks

The importer closes GnuCash in a `finally` block. If an earlier crash leaves a stale SQLite lock, close GnuCash and all import processes, then run:

```bash
python clear_gnclock.py portfolio-sqlite.gnucash
```

Only clear a lock when no process genuinely has the book open.
