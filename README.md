# GnuCash CSV Importer

A Python CLI importer for **Revolut**, **Bank of Scotland**, and **Alpha Bank Greece** CSV exports into a SQLite-backed GnuCash book.

The importer reads your existing account tree, suggests destination accounts, creates balanced double-entry transactions, tracks imported or intentionally skipped rows, detects likely duplicate ledger entries, and creates timestamped database backups before making changes.

> **Important:** This tool modifies a GnuCash SQLite book. Test with `--dry-run` first and keep independent backups of important accounting data.

## Features

- Native CSV support for:
  - Revolut
  - Bank of Scotland
  - Alpha Bank Greece
  - Generic CSV files with date, payee, and amount fields
- Balanced double-entry transactions between a selected source bank account and a destination account.
- Correct `post_date` from the source CSV.
- GBP, EUR, and USD support.
- Account suggestions based on mappings, transaction history, account names, and keywords.
- Persistent payee-to-account mappings.
- Manual account-path entry with terminal tab completion.
- Source-account search across GnuCash asset-like account types.
- Automatic exclusion of internal GnuCash template and `Orphan-*` accounts.
- Duplicate checks against:
  - Rows previously handled by this importer.
  - Transactions already present in the selected GnuCash source account.
- Skip a row temporarily or permanently mark it as handled.
- Timestamped SQLite database backup before the first write in each run.
- Configurable backup retention.
- Account-tree export and imported-record date validation.

## Requirements

- Python 3.9 or newer
- A GnuCash book stored using the **SQLite** backend
- `piecash`
- SQLAlchemy compatible with your installed piecash version

Example setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install piecash "SQLAlchemy<2"
```

Exact SQLAlchemy requirements can vary with the piecash release you use. Test against a copy of your book first.

## GnuCash Format

`piecash` works with SQL-backed GnuCash books, not ordinary XML books. The importer expects a SQLite `.gnucash` file.

To convert an XML book:

1. Open the XML book in GnuCash.
2. Select **File → Save As…**.
3. Select the **sqlite3** format.
4. Save a new copy, for example `portfolio-sqlite.gnucash`.

Keep the original XML file as an independent backup.

## Quick Start

Run a review-only import first:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --dry-run
```

For a real interactive import, omit `--dry-run`:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv"
```

The importer asks which GnuCash account the file belongs to. This is the **source account**: the bank account whose statement you exported.

For example:

```text
Assets:Current Assets:Revolut GBP
Assets:Current Assets:BOS Salary Account
Assets:Current Assets:Alpha Bank Savings
```

## Database Backups

### Automatic Backups

On every non-dry-run execution that is about to change the GnuCash book, the importer creates exactly one backup **before the first write**.

This includes writes caused by either:

- Creating a new account.
- Committing prepared transactions.

No backup is created during `--dry-run`, because dry-run never writes to the database.

Backups are placed in a `backups/` directory next to the SQLite GnuCash book:

```text
finance/
├── portfolio-sqlite.gnucash
└── backups/
    ├── portfolio-sqlite.backup-20260823-032501.gnucash
    ├── portfolio-sqlite.backup-20260823-041902.gnucash
    └── portfolio-sqlite.backup-20260824-090317.gnucash
```

The filename timestamp uses local time in this format:

```text
YYYYMMDD-HHMMSS
```

### Backup Retention

By default, the importer keeps the **10 newest** backups and deletes older importer-created backups after a new backup is created.

Set a different count with `--keep-backups`:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --keep-backups=30
```

Set `0` to disable automatic pruning and keep all backups:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --keep-backups=0
```

> Backup pruning only affects files created by this importer in the `backups/` directory whose names match the expected backup pattern. It does not delete arbitrary files.

### Restoring a Backup

Close GnuCash and all importer processes first. Then replace the live database with a chosen backup:

```bash
cp backups/portfolio-sqlite.backup-20260823-032501.gnucash portfolio-sqlite.gnucash
```

Make a copy of the current file before restoring if you may need to return to it:

```bash
cp portfolio-sqlite.gnucash portfolio-sqlite.gnucash.before-restore
```

Automatic backups are a convenience feature, not a complete disaster-recovery strategy. Keep independent backups using Time Machine, cloud backup, versioned storage, or your preferred backup system.

## Source Account Selection

Every import transaction has two sides:

1. The source account represented by the statement.
2. The destination expense, income, transfer, or other category account.

For example, a Revolut card payment of £35.08 may create:

```text
Assets:Current Assets:Revolut GBP      -35.08 GBP
Expenses:Food:Restaurant                 35.08 GBP
```

The values sum to zero, satisfying GnuCash double-entry rules.

Specify the source account directly to avoid the interactive picker:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --source-account="Assets:Current Assets:Revolut GBP" \
  --dry-run
```

The picker considers GnuCash account types:

```text
ASSET, BANK, CASH, CHECKING, STOCK, MUTUAL, RECEIVABLE
```

This matters because accounts made with the GnuCash UI may be typed as `BANK`, while older scripts might only look for generic `ASSET` accounts.

Internal accounts are excluded:

- `Orphan-GBP`, `Orphan-EUR`, etc. — unresolved/suspense import artifacts.
- GUID-like template accounts used internally for scheduled transactions.

If you do not see an account, type a partial search term:

```text
Enter number, exact account path (Tab to autocomplete), or search term: alpha
```

## Supported Formats

### Revolut

Supported modern format:

```text
Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
```

| Column | Imported as |
|---|---|
| `Completed Date` | Transaction date; falls back to `Started Date` |
| `Description` | Payee |
| `Amount` | Signed amount |
| `Currency` | Currency |
| `Type` + `State` | Memo |

Pending transactions are skipped by default. Include them with:

```bash
--include-pending
```

### Bank of Scotland

Supported CSV columns:

```text
Date,Description,Type,Money In (£),Money Out (£),Balance (£)
```

| Column | Imported as |
|---|---|
| `Date` | Transaction date, parsed as `DD Mon YY` |
| `Description` | Payee |
| `Money In (£)` | Positive amount |
| `Money Out (£)` | Negative amount |
| `Type` | Expanded memo, such as `DD` → `Direct Debit` |

Bank of Scotland PDF statements are not importable directly. Download a CSV from online banking first.

### Alpha Bank Greece

Alpha Bank exports are detected automatically from their Greek header row and semicolon delimiter. The file may have metadata before the actual header:

```text
Τίτλος;Κινήσεις Λογαριασμού: ;GR6901401190119002101336675;;;;;
...
Α/Α;Ημ/νία;Αιτιολογία;Κατάστημα;Τοκισμός από;Αρ. συναλλαγής;Ποσό;Πρόσημο ποσού
```

| Greek column | Meaning | Imported as |
|---|---|---|
| `Ημ/νία` | Date | Transaction date (`DD/MM/YYYY`) |
| `Αιτιολογία` | Description | Payee |
| `Αρ. συναλλαγής` | Transaction reference | Memo |
| `Ποσό` | Amount | Amount parsed with European formatting |
| `Πρόσημο ποσού` | Amount sign | `Χ` debit = negative; `Π` credit = positive |

Examples:

```text
135,10;Χ    -> -135.10 EUR
1.100,00;Π  -> +1100.00 EUR
1.532,76;Χ  -> -1532.76 EUR
```

The Alpha Bank mapper defaults currency to EUR.

## Account Matching

The importer suggests destination accounts through these layers:

1. Persistent saved payee mappings.
2. Existing GnuCash transaction history.
3. Exact and substring account-name matches.
4. Keyword/token overlap against account names.

Example: `Charing Cross Car Park` may suggest parking accounts that share terms such as `car` and `park`, even if the exact payee was never seen before.

Generic history words such as `transfer` are down-weighted so distinctive words, such as a surname, are more useful matches.

### Persistent Mappings

Saved mappings are written to:

```text
.payee_account_mappings.json
```

List them:

```bash
python ./gnucash_importer.py --list-mappings
```

Clear them:

```bash
python ./gnucash_importer.py --clear-mappings
```

## Creating and Entering Accounts

When suggestions point to a category, creating an account uses that category as a starting point. For example, parking suggestions under `Expenses:Auto:Parking:*` can produce:

```text
Expenses:Auto:Charing Cross Car Park
```

New accounts are saved immediately before their related mapping is persisted.

Use manual account path entry to select an existing account or create a new one. Compatible terminals support tab completion.

## Skipping Rows

The interactive selection menu has two skip choices:

```text
Skip and mark as imported (never ask again)
Skip for now (ask again next run)
```

### Permanent Skip

A permanent skip creates no GnuCash transaction. It writes a record containing `"skipped": true` to:

```text
.imported_transactions.json
```

The source row will then report as already handled on future imports.

### Temporary Skip

A temporary skip writes nothing. The row is offered again next time.

## Duplicate Detection

The importer uses two duplicate layers.

### Importer History

It hashes each row using:

```text
CSV date | payee | amount
```

If that hash already exists in `.imported_transactions.json`, the row is skipped.

### Existing Ledger Check

The importer also indexes transactions already in the selected source account using:

```text
(post_date, signed source-account split amount)
```

This finds transactions entered manually or imported outside this script.

When a candidate is found:

```text
POSSIBLE DUPLICATE: an existing transaction on 2026-08-19 for the same amount is already in the ledger:
  'Existing description'
Is this the same transaction? [Y]es (skip) / n (import anyway):
```

Use `--no-ledger-check` to disable this extra check when you know the file contains entirely new data.

## Dry Run

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --dry-run
```

Dry-run opens the book read-only and does not:

- Create backups.
- Create accounts.
- Save mappings.
- Mark skipped rows as imported.
- Write transactions.

## Auto-Accept

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --source-account="Assets:Current Assets:Revolut GBP" \
  --auto-accept
```

This chooses the top account suggestion automatically. Use only after validating matching quality with dry-runs.

## Validation

Use `validate_import.py` to compare imported records against their original CSV, particularly for date validation:

```bash
python validate_import.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv"
```

Fix identified post-date mismatches:

```bash
python validate_import.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --fix
```

## Account Export

Export account metadata to JSON:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --export-accounts
```

## SQLite Lock Recovery

GnuCash SQLite uses an internal `gnclock` table. The importer closes the book in a `finally` block and prints:

```text
GnuCash book closed - lock released.
```

If a stale lock remains after a historical crash, close GnuCash and all importer processes, then run:

```bash
python clear_gnclock.py portfolio-sqlite.gnucash
```

Only clear a lock when no other process is genuinely using the book.

## Persistent Files

| File | Purpose |
|---|---|
| `.imported_transactions.json` | Records imported and permanently skipped rows |
| `.payee_account_mappings.json` | Stores payee-to-account mappings |
| `.transaction_history_analysis.json` | Cached transaction history analysis |
| `accounts.json` | Optional account-tree export |
| `backups/` | Timestamped SQLite database backups |

Keep these files in the same project directory as the importer if you want matching and duplicate history to persist consistently.

## Troubleshooting

### `Invalid argument(s) 'backend' sent to create_engine()`

Remove obsolete `backend="xml"` or `check_exists=False` parameters from `piecash.open_book()`. The script expects a SQLite book.

### `GncImbalanceError`

The transaction has unbalanced splits. The importer creates a source-account split and an equal-and-opposite destination split, so this should not occur unless the script has been modified or a source account is invalid.

### An account is missing from the source list

Search by a partial name at the prompt, for example:

```text
alpha
revolut
bos
```

The importer includes `BANK` accounts as well as generic `ASSET` accounts, so GnuCash savings/checking accounts should appear.

### `Orphan-*` or GUID/template accounts appear

Use the current version of the importer. The source-account selector excludes:

- `Orphan-<currency>` unresolved-split accounts.
- Scheduled-transaction template accounts.

## Typical Commands

### Revolut review

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="revolut-transactions.csv" \
  --source-account="Assets:Current Assets:Revolut GBP" \
  --dry-run
```

### Bank of Scotland import

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="bos-transactions.csv" \
  --source-account="Assets:Current Assets:BOS Salary Account"
```

### Alpha Bank Greece import

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="accounts.csv" \
  --source-account="Assets:Current Assets:Alpha Bank Savings" \
  --dry-run
```
