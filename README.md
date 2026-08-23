# GnuCash CSV Importer and Reporting Toolkit

A Python toolkit for importing bank transaction CSV files into a SQLite-backed GnuCash book and producing income, expense, and cash-flow reports.

## Included Scripts

| Script | Purpose |
|---|---|
| `gnucash_importer.py` | Imports bank CSV transactions into GnuCash with account suggestions, duplicate checks, backups, and mappings. |
| `income_expense_report.py` | Creates read-only monthly income, expense, and cash-flow reports. Supports native currencies and consolidated reporting using GnuCash’s Price Database. |
| `validate_import.py` | Validates recorded imports against source CSV data and can repair transaction-date mismatches. |
| `clear_gnclock.py` | Clears stale SQLite GnuCash lock rows after confirming no genuine writer is active. |

> **Important:** `gnucash_importer.py` modifies the book. Always use `--dry-run` first and keep independent backups. `income_expense_report.py` is read-only.

## Requirements

- Python 3.9 or newer
- A GnuCash book stored using the **SQLite** backend
- Importer dependencies:
  - `piecash`
  - A compatible SQLAlchemy version
- Optional chart dependency:
  - `matplotlib`

Example setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install piecash "SQLAlchemy<2"
pip install matplotlib  # required only for --plot
```

## GnuCash Database Format

The importer requires a GnuCash SQLite book. If your primary book is XML:

1. Open it in GnuCash.
2. Choose **File → Save As…**.
3. Select **sqlite3**.
4. Save a new copy such as `portfolio-sqlite.gnucash`.

Keep the XML original as an independent backup.

# Importer

## Basic Workflow

Review without writing:

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

The script asks for the **source account** represented by the statement, for example:

```text
Assets:Current Assets:Revolut GBP
Assets:Current Assets:BOS Salary Account
Assets:Current Assets:Alpha Bank Savings
```

Avoid the prompt with:

```bash
--source-account="Assets:Current Assets:Revolut GBP"
```

Every imported transaction is a balanced double-entry transaction:

```text
Assets:Current Assets:Revolut GBP      -35.08 GBP
Expenses:Food:Restaurant                 35.08 GBP
```

## Database Backups

Before the first write in a non-dry-run import, the importer makes one timestamped SQLite backup.

Backups are stored next to the database:

```text
backups/
├── portfolio-sqlite.backup-20260823-032501.gnucash
├── portfolio-sqlite.backup-20260823-041902.gnucash
└── portfolio-sqlite.backup-20260824-090317.gnucash
```

One backup is made per write run, even when the run both creates accounts and imports transactions.

Default retention is 10 backups:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --keep-backups=30
```

Retain every backup permanently:

```bash
--keep-backups=0
```

No backup is made during `--dry-run` because no database write occurs.

To restore a backup, close GnuCash and all import processes first:

```bash
cp backups/portfolio-sqlite.backup-20260823-032501.gnucash portfolio-sqlite.gnucash
```

## Supported Bank Formats

### Revolut

Supported modern header:

```text
Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
```

- `Completed Date` is the transaction date, falling back to `Started Date`.
- `Description` becomes payee.
- `Amount` is signed directly.
- `Currency` selects the transaction currency.
- `Type` and `State` form the memo.

Pending rows are excluded by default. Include them with:

```bash
--include-pending
```

### Bank of Scotland

Supported columns:

```text
Date,Description,Type,Money In (£),Money Out (£),Balance (£)
```

- `Money In (£)` is positive.
- `Money Out (£)` is negative.
- Dates parse as `DD Mon YY`, such as `03 Aug 26`.
- The transaction type is expanded into the memo, for example `DD` becomes `Direct Debit`.

Download CSV data from online banking. PDF statements are not imported directly.

### Alpha Bank Greece

Alpha Bank exports use semicolon delimiters, Greek headers, European number formatting, and a metadata preamble before the actual header:

```text
Τίτλος;Κινήσεις Λογαριασμού: ;GR6901401190119002101336675;;;;;
...
Α/Α;Ημ/νία;Αιτιολογία;Κατάστημα;Τοκισμός από;Αρ. συναλλαγής;Ποσό;Πρόσημο ποσού
```

| Field | Meaning | Import behaviour |
|---|---|---|
| `Ημ/νία` | Date | Parsed as `DD/MM/YYYY` |
| `Αιτιολογία` | Description | Payee |
| `Αρ. συναλλαγής` | Transaction reference | Memo |
| `Ποσό` | Amount | European decimal parsing |
| `Πρόσημο ποσού` | Sign | `Χ` debit = negative; `Π` credit = positive |

Examples:

```text
135,10;Χ    -> -135.10 EUR
1.100,00;Π  -> +1100.00 EUR
1.532,76;Χ  -> -1532.76 EUR
```

Alpha Bank rows are treated as EUR by default.

## Account Selection

The source-account picker includes GnuCash asset-like types:

```text
ASSET, BANK, CASH, CHECKING, STOCK, MUTUAL, RECEIVABLE
```

This matters because accounts created in GnuCash may be type `BANK`, not generic `ASSET`.

The picker excludes internal accounts:

- `Orphan-GBP`, `Orphan-EUR`, and similar unresolved-split accounts.
- GUID-like scheduled-transaction template accounts.

Search by partial name if needed:

```text
Enter number, exact account path (Tab to autocomplete), or search term: alpha
```

## Account Matching

Destination suggestions use:

1. Persistent payee mappings.
2. Existing transaction history.
3. Exact and partial account-name matches.
4. Keyword/token overlap against account names.

Mappings are stored in:

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

## Duplicate Detection

The importer checks duplicates in two ways.

### Importer History

Rows are hashed from:

```text
CSV date | payee | amount
```

Rows already in `.imported_transactions.json` are skipped.

### Existing Ledger Check

The importer indexes transactions already in the selected source account by:

```text
(post_date, signed source-account split amount)
```

This catches manually entered transactions or transactions imported by another method.

Disable this additional check when the CSV is known to be entirely new:

```bash
--no-ledger-check
```

## Skip Options

The interactive menu has two skip choices:

```text
Skip and mark as imported (never ask again)
Skip for now (ask again next run)
```

Permanent skips do not create a GnuCash transaction. They are recorded with `"skipped": true` in `.imported_transactions.json`.

## Dry Run and Auto-Accept

Dry-run performs no writes:

```bash
--dry-run
```

It does not create backups, accounts, mappings, skipped records, or transactions.

Auto-accept chooses the strongest suggestion without per-row prompts:

```bash
--auto-accept
```

Use it only after reviewing matching behaviour with dry-runs.

# Income, Expense, and Cash-Flow Reports

`income_expense_report.py` is a **read-only** report generator.

- Opens SQLite using read-only mode.
- Does not use piecash.
- Does not create a GnuCash lock.
- Does not write prices, transactions, accounts, mappings, or backups.

## Native Currency Reports

Generate separate monthly reports for every currency found in a year:

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026
```

Each report contains twelve monthly rows and an annual total:

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

Income is displayed as positive even though GnuCash normally stores income credits as negative account quantities. Expenses are displayed as positive. Refunds and reversals reduce the relevant total naturally.

Filter to one native currency:

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026 \
  --currency=EUR
```

## Consolidated Currency Reports

Use `--report-currency` to convert all income and expenses into one selected currency:

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026 \
  --report-currency=GBP
```

The report uses the existing **GnuCash Price Database** only. It does not fetch internet rates or change the book.

For each split:

1. The report identifies the source account’s currency.
2. It searches the Price Database for the closest direct exchange rate to the transaction `post_date`.
3. If no direct rate exists, it searches for the reverse pair and inverts it.
4. It converts the split into the selected report currency.

For example, if a €100 expense has an EUR→GBP price of `0.8512`:

```text
100.00 EUR × 0.8512 = 85.12 GBP
```

### Rate Tolerance

By default, the nearest stored rate must be within 7 days of the transaction date:

```bash
--rate-tolerance-days=7
```

Use a stricter requirement, for example exact-date-only matching:

```bash
--rate-tolerance-days=0
```

Use a wider range only if appropriate for your accounting method:

```bash
--rate-tolerance-days=30
```

### Missing Rates

Default behaviour is `warn`: rows lacking a usable exchange rate are excluded from consolidated totals and reported in the terminal.

```bash
--missing-rate=warn
```

Other modes:

```bash
--missing-rate=skip
--missing-rate=fail
```

`fail` stops immediately when a required rate is missing. This is useful for strict/reproducible financial reporting.

The report intentionally does **not** silently retrieve current online rates for historical transactions. Current rates are inappropriate for historical monthly cash flow and would make a report non-reproducible. Populate GnuCash’s Price Database with appropriate historical currency rates, then re-run the report.

## Conversion Audit CSV

For consolidated reports, create an audit record of every conversion:

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026 \
  --report-currency=GBP \
  --rate-tolerance-days=7 \
  --conversion-audit-csv="conversion-audit-2026-gbp.csv"
```

The audit CSV includes:

```text
post_date
account
account_type
description
source_amount
source_currency
target_currency
rate
rate_date
rate_source
distance_days
converted_amount
status
reason
```

`rate_source` is one of:

```text
identity
ognucash-db-direct
gnucash-db-inverted
```

A missing rate creates an audit row with `status=missing` and an explanatory `reason`.

## Category Breakdown

Show annual totals by income and expense account:

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026 \
  --report-currency=GBP \
  --detail
```

## CSV Summary Export

Export monthly totals to CSV:

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026 \
  --report-currency=GBP \
  --csv-output="income-expense-2026-gbp.csv"
```

## Cash-Flow Plot

Generate a chart with income bars, expense bars, and net-cash-flow line:

```bash
python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026 \
  --report-currency=GBP \
  --plot="cashflow-2026-gbp.png"
```

Install matplotlib if needed:

```bash
pip install matplotlib
```

A plot requires one output currency. Use either `--currency` for native reporting or `--report-currency` for a converted consolidated report.

# Validation and Recovery

## Validate Imports

```bash
python validate_import.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv"
```

Apply date corrections:

```bash
python validate_import.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --fix
```

## Export Accounts

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --export-accounts
```

## Stale SQLite Locks

After confirming GnuCash and all importer processes are closed:

```bash
python clear_gnclock.py portfolio-sqlite.gnucash
```

Do not clear a lock while another process is genuinely writing to the book.

# Persistent Files

| File or directory | Purpose |
|---|---|
| `.imported_transactions.json` | Imported and permanently skipped rows |
| `.payee_account_mappings.json` | Payee-to-account mappings |
| `.transaction_history_analysis.json` | Cached transaction-history analysis |
| `accounts.json` | Optional account-tree export |
| `backups/` | Timestamped importer database backups |

## Typical Commands

### Revolut Review

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="revolut-transactions.csv" \
  --source-account="Assets:Current Assets:Revolut GBP" \
  --dry-run
```

### Bank of Scotland Import

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="bos-transactions.csv" \
  --source-account="Assets:Current Assets:BOS Salary Account"
```

### Alpha Bank Greece Review

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="accounts.csv" \
  --source-account="Assets:Current Assets:Alpha Bank Savings" \
  --dry-run
```

## Troubleshooting

### `Invalid argument(s) 'backend' sent to create_engine()`

Remove obsolete `backend="xml"` or `check_exists=False` arguments from `piecash.open_book()`. The importer expects SQLite.

### `GncImbalanceError`

A transaction has unbalanced splits. The importer creates a source split and equal/opposite destination split, so this normally indicates a modified script or invalid account setup.

### Source Account Not Found

Search by a partial name at the source-account prompt:

```text
alpha
revolut
bos
```

The importer considers both `BANK` and generic `ASSET` types.

### Internal Accounts Appear

The current importer excludes `Orphan-*` and scheduled-transaction template accounts from source selection. Ensure you are using the latest version.
