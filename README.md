# GnuCash CSV Importer

A Python toolkit for importing bank transactions into a SQLite-backed GnuCash book and reporting on income, expenses, and cash flow.

It includes two primary scripts:

- `gnucash_importer.py` — imports CSV bank transactions from Revolut, Bank of Scotland, Alpha Bank Greece, or a generic CSV format.
- `income_expense_report.py` — generates read-only monthly income/expense/cash-flow reports for a selected year.

Additional helper scripts may be present:

- `validate_import.py` — validates imported transactions against a source CSV and can correct post-date mismatches.
- `clear_gnclock.py` — removes stale SQLite GnuCash locks after confirming no real writer is active.

> **Important:** The importer modifies a GnuCash SQLite book. Always use `--dry-run` first and retain independent backups of important accounting data.

## Features

### Importer

- Native CSV support for:
  - Revolut
  - Bank of Scotland
  - Alpha Bank Greece
  - Generic date/payee/amount CSV files
- Balanced double-entry transactions between the statement source account and selected destination account.
- CSV transaction date used as GnuCash `post_date`.
- Supports GBP, EUR, and USD accounts.
- Account suggestions from mappings, history, account names, and keyword overlap.
- Persistent payee-to-account mappings.
- Manual account path entry with tab completion in compatible terminals.
- Duplicate detection against both importer history and existing GnuCash transactions.
- Permanent and temporary skip modes.
- Timestamped pre-write SQLite database backups with configurable retention.
- Excludes internal GnuCash template accounts and `Orphan-*` accounts from source-account selection.

### Reporting

- Read-only monthly income, expense, and net cash-flow report.
- Runtime year selection with `--year`.
- Separate reports per currency by default, avoiding invalid GBP/EUR/USD totals.
- Optional one-currency reporting with `--currency`.
- Optional annual breakdown by income/expense account.
- Optional CSV export.
- Optional PNG cash-flow chart.
- Direct SQLite read-only access: no piecash dependency and no GnuCash lock is created.

## Requirements

- Python 3.9 or newer.
- A GnuCash book stored using the **SQLite** backend.
- Importer dependencies:
  - `piecash`
  - SQLAlchemy compatible with the installed piecash version
- Optional reporting-chart dependency:
  - `matplotlib`

Example environment setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install piecash "SQLAlchemy<2"
pip install matplotlib  # only needed for --plot
```

The reporting script itself uses only Python’s standard library unless you request a plot.

## GnuCash Format

`piecash` works with SQL-backed GnuCash books, not ordinary XML books. The importer therefore expects a SQLite `.gnucash` file.

To convert an XML book:

1. Open the XML book in GnuCash.
2. Select **File → Save As…**.
3. Choose the **sqlite3** format.
4. Save a new copy, for example `portfolio-sqlite.gnucash`.

Keep the original XML file as an independent backup.

## Import Quick Start

Review a file without writing anything:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --dry-run
```

Run an interactive import:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv"
```

The importer asks which GnuCash account is represented by the statement. This is the **source account** — for example:

```text
Assets:Current Assets:Revolut GBP
Assets:Current Assets:BOS Salary Account
Assets:Current Assets:Alpha Bank Savings
```

Specify it explicitly to avoid the prompt:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --source-account="Assets:Current Assets:Revolut GBP" \
  --dry-run
```

## Database Backups

### Automatic Pre-Write Backup

On every non-dry-run session that is about to change the book, the importer creates exactly one timestamped backup **before the first write**.

A backup is made before either:

- Creating a new GnuCash account.
- Saving imported transactions.

No backup is created during `--dry-run` because dry-run never writes.

Backups are stored beside the GnuCash database:

```text
finance/
├── portfolio-sqlite.gnucash
└── backups/
    ├── portfolio-sqlite.backup-20260823-032501.gnucash
    ├── portfolio-sqlite.backup-20260823-041902.gnucash
    └── portfolio-sqlite.backup-20260824-090317.gnucash
```

The timestamp format is:

```text
YYYYMMDD-HHMMSS
```

### Backup Retention

The default is to keep the newest **10** importer-created backups.

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --keep-backups=30
```

Use `0` to retain every backup permanently:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --keep-backups=0
```

Pruning affects only importer-created files matching the expected timestamped backup naming pattern inside `backups/`.

### Restore a Backup

Close GnuCash and the importer first. Then replace the live database with a chosen backup:

```bash
cp backups/portfolio-sqlite.backup-20260823-032501.gnucash portfolio-sqlite.gnucash
```

Before restoring, preserve the current version if required:

```bash
cp portfolio-sqlite.gnucash portfolio-sqlite.gnucash.before-restore
```

Automatic backups are a convenience feature, not a complete disaster-recovery plan. Use Time Machine, cloud backup, versioned storage, or another independent backup mechanism as well.

## Source Account Selection

Every imported transaction has two sides:

1. The source account represented by the bank statement.
2. A destination account such as an income, expense, transfer, or category account.

For example, a Revolut card payment might create:

```text
Assets:Current Assets:Revolut GBP      -35.08 GBP
Expenses:Food:Restaurant                 35.08 GBP
```

The splits sum to zero, satisfying GnuCash double-entry rules.

The importer includes all GnuCash asset-like account types:

```text
ASSET, BANK, CASH, CHECKING, STOCK, MUTUAL, RECEIVABLE
```

This is important because GnuCash commonly gives savings/checking accounts the `BANK` type, not the generic `ASSET` type.

The selector excludes internal accounts such as:

- `Orphan-GBP`, `Orphan-EUR`, etc. — unresolved split/suspense accounts.
- GUID-like template accounts used internally for scheduled transactions.

If an account is not listed, type a partial search term at the prompt:

```text
Enter number, exact account path (Tab to autocomplete), or search term: alpha
```

## Supported Bank Formats

### Revolut

Supported modern columns:

```text
Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
```

| CSV field | Imported field |
|---|---|
| `Completed Date` | Transaction date; falls back to `Started Date` |
| `Description` | Payee |
| `Amount` | Signed amount |
| `Currency` | Currency |
| `Type` + `State` | Memo |

Pending transactions are skipped by default. Include them using:

```bash
--include-pending
```

### Bank of Scotland

Supported CSV columns:

```text
Date,Description,Type,Money In (£),Money Out (£),Balance (£)
```

| CSV field | Imported field |
|---|---|
| `Date` | Date, parsed as `DD Mon YY` |
| `Description` | Payee |
| `Money In (£)` | Positive amount |
| `Money Out (£)` | Negative amount |
| `Type` | Human-readable memo, for example `DD` → `Direct Debit` |

Bank of Scotland PDF statements are not imported directly. Download a CSV from online banking first.

### Alpha Bank Greece

Alpha Bank exports are detected from their Greek header row and semicolon delimiter. The file includes metadata before the real header:

```text
Τίτλος;Κινήσεις Λογαριασμού: ;GR6901401190119002101336675;;;;;
...
Α/Α;Ημ/νία;Αιτιολογία;Κατάστημα;Τοκισμός από;Αρ. συναλλαγής;Ποσό;Πρόσημο ποσού
```

| Greek field | Meaning | Imported as |
|---|---|---|
| `Ημ/νία` | Date | Transaction date (`DD/MM/YYYY`) |
| `Αιτιολογία` | Description/reason | Payee |
| `Αρ. συναλλαγής` | Transaction reference | Memo |
| `Ποσό` | Amount | European numeric format |
| `Πρόσημο ποσού` | Sign | `Χ` debit = negative; `Π` credit = positive |

Examples:

```text
135,10;Χ    -> -135.10 EUR
1.100,00;Π  -> +1100.00 EUR
1.532,76;Χ  -> -1532.76 EUR
```

Alpha Bank transactions are treated as EUR by default.

## Account Matching

The importer proposes destination accounts from:

1. Persistent payee mappings.
2. Existing GnuCash transaction history.
3. Exact and substring account-name matches.
4. Keyword/token overlap against account names.

It down-weights broad history terms such as `transfer` and `payment`, allowing more distinctive terms such as merchants or surnames to have greater influence.

Saved mappings live in:

```text
.payee_account_mappings.json
```

List mappings:

```bash
python ./gnucash_importer.py --list-mappings
```

Clear mappings:

```bash
python ./gnucash_importer.py --clear-mappings
```

## Creating and Selecting Accounts

When suggestions point to a category, the importer can suggest a logical new account path. For example, parking suggestions under `Expenses:Auto:Parking:*` can lead to:

```text
Expenses:Auto:Charing Cross Car Park
```

New accounts are saved immediately before their mapping is persisted.

The manual selector can accept an account path directly. In compatible terminals, press Tab to complete a path and press Tab twice to see alternatives.

## Skipping Transactions

The interactive selector distinguishes between:

```text
Skip and mark as imported (never ask again)
Skip for now (ask again next run)
```

### Permanent Skip

A permanent skip creates no transaction in GnuCash. Instead, it writes a record with `"skipped": true` to:

```text
.imported_transactions.json
```

The row will not be offered again in a future import.

### Temporary Skip

A temporary skip writes nothing. The transaction will be offered again next time.

## Duplicate Detection

The importer uses two independent checks.

### Importer History

Rows are hashed from:

```text
CSV date | payee | amount
```

If a hash is already present in `.imported_transactions.json`, the row is skipped.

### Existing Ledger Check

The importer also indexes transactions already in the selected source account using:

```text
(post_date, signed source-account split amount)
```

This detects transactions entered manually or through another GnuCash import workflow.

When it finds a candidate:

```text
POSSIBLE DUPLICATE: an existing transaction on 2026-08-19 for the same amount is already in the ledger:
  'Existing description'
Is this the same transaction? [Y]es (skip) / n (import anyway):
```

Disable ledger checking when a file is known to contain entirely new data:

```bash
--no-ledger-check
```

## Dry Run

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --dry-run
```

Dry-run does not create backups, accounts, mappings, skipped records, or transactions.

## Auto-Accept

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --source-account="Assets:Current Assets:Revolut GBP" \
  --auto-accept
```

Use auto-accept only after reviewing matching quality with dry-runs.

## Income and Expense Reporting

`income_expense_report.py` creates a monthly report from the existing GnuCash SQLite database.

It is intentionally **read-only**:

- It opens SQLite with `mode=ro`.
- It does not use piecash.
- It creates no GnuCash lock.
- It does not create database backups because it never writes.

The report uses GnuCash account-level **quantity** values instead of transaction-level value fields. This matters for a multi-currency book: quantity is recorded in the account’s own commodity, so EUR expense accounts report in EUR and GBP accounts report in GBP.

### Basic Yearly Report

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026
```

The script prints a separate table for each currency found in the selected year. This avoids incorrectly combining EUR, GBP, and USD into one total.

Each table includes all twelve months, then a final annual total:

```text
Month                     Income             Expenses           Net cash flow
----------------------------------------------------------------------------
Jan                 2,500.00 GBP        1,900.00 GBP             600.00 GBP
Feb                 2,400.00 GBP        1,750.00 GBP             650.00 GBP
...
YEAR TOTAL          30,000.00 GBP       22,500.00 GBP           7,500.00 GBP
```

The formula is:

```text
Net cash flow = Income - Expenses
```

Income is shown as positive for readability even though GnuCash normally stores income credits as negative splits internally. Expenses are shown as positive. Refunds and reversals naturally reduce the corresponding totals.

### Filter to One Currency

For GBP:

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026 \
  --currency=GBP
```

For EUR, for example your Alpha Bank reporting:

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026 \
  --currency=EUR
```

### Annual Account Breakdown

Use `--detail` to print annual totals by individual income and expense account:

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026 \
  --currency=GBP \
  --detail
```

Example output:

```text
Account                                              Annual total
----------------------------------------------------------------------------
Expenses:Food:Groceries                                4,200.00 GBP
Expenses:Auto:Parking                                  1,180.00 GBP
Income:Salary                                         29,500.00 GBP
```

### CSV Export

Export the monthly report to a spreadsheet-friendly CSV:

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026 \
  --currency=EUR \
  --csv-output="income-expense-2026-eur.csv"
```

The CSV contains 12 monthly rows plus a final `TOTAL` row.

### Cash-Flow Chart

Generate a PNG chart with income bars, expense bars, and a net cash-flow line:

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026 \
  --currency=GBP \
  --plot="cashflow-2026-gbp.png"
```

Install matplotlib first if necessary:

```bash
pip install matplotlib
```

A chart must use one currency. If your selected year contains more than one currency, specify `--currency` explicitly.

## Import Validation

Use `validate_import.py` to compare recorded imports against their original source CSV, particularly for transaction-date validation:

```bash
python validate_import.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv"
```

Apply detected post-date corrections:

```bash
python validate_import.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --fix
```

## Account Export

Export the account tree to JSON:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --export-accounts
```

## SQLite Lock Recovery

GnuCash SQLite uses an internal `gnclock` table. The importer closes its book in a `finally` block and normally prints:

```text
GnuCash book closed - lock released.
```

For a stale lock left by an earlier crash, close GnuCash and every importer process, then run:

```bash
python clear_gnclock.py portfolio-sqlite.gnucash
```

Only clear a lock when no process is genuinely using the book.

## Persistent Files

| File or directory | Purpose |
|---|---|
| `.imported_transactions.json` | Imported and permanently skipped rows |
| `.payee_account_mappings.json` | Payee-to-account mappings |
| `.transaction_history_analysis.json` | Cached transaction-history analysis |
| `accounts.json` | Optional account-tree export |
| `backups/` | Timestamped pre-write SQLite database backups |

Keep these files in the same project directory if you want mappings and importer history to remain consistent.

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

### Alpha Bank Greece review

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="accounts.csv" \
  --source-account="Assets:Current Assets:Alpha Bank Savings" \
  --dry-run
```

## Troubleshooting

### `Invalid argument(s) 'backend' sent to create_engine()`

Remove obsolete `backend="xml"` or `check_exists=False` parameters from `piecash.open_book()`. The importer expects a SQLite book.

### `GncImbalanceError`

A transaction has unbalanced splits. The importer creates a source-account split and an equal-and-opposite destination split, so this should not occur unless the script has been changed or the source account is invalid.

### An account is missing from the source list

Search by a partial name at the account prompt:

```text
alpha
revolut
bos
```

The importer includes GnuCash `BANK` accounts in addition to generic `ASSET` accounts.

### `Orphan-*` or GUID/template entries appear

Use the latest script. Internal unresolved-split accounts and scheduled-transaction template accounts are excluded from source account selection.
