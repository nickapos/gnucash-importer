# GnuCash CSV Importer

A Python CLI importer for posting bank transactions from **Revolut**, **Bank of Scotland**, and **Alpha Bank Greece** CSV exports into a SQLite-backed GnuCash book.

The importer is designed for an existing GnuCash account tree. It reads the book, learns from prior transactions, suggests likely destination accounts, creates balanced double-entry transactions, tracks what it has processed, and checks the ledger for likely duplicates before importing.

> **Important:** This tool operates on a GnuCash SQLite book. Before using it against important financial data, make a backup and run a dry-run first.

## Features

- Supports CSV exports from:
  - Revolut
  - Bank of Scotland
  - Alpha Bank Greece
  - Generic CSV files containing date, payee, and amount fields
- Reads your GnuCash account tree and uses it for interactive account selection.
- Supports GBP, EUR, and USD transactions.
- Creates properly balanced double-entry transactions.
- Uses the CSV transaction date as GnuCash `post_date`.
- Selects a source bank account for each import batch.
- Learns persistent payee-to-account mappings in JSON.
- Suggests destination accounts from:
  - Previously saved mappings
  - Existing GnuCash transaction history
  - Exact and fuzzy account-name matches
  - Keyword/token matches against account names
- Detects likely duplicates already present in the GnuCash ledger, including transactions that were not imported by this script.
- Lets you skip transactions either temporarily or permanently.
- Excludes GnuCash internal `Orphan-*` and scheduled-transaction template accounts from source-account selection.
- Supports account-path tab completion for manual account selection in compatible terminals.
- Exports account metadata to JSON.
- Includes support for validating imported records against their original CSV.

## Requirements

- Python 3.9 or newer
- A GnuCash book stored using the **SQLite** backend
- `piecash`
- SQLAlchemy compatible with your installed piecash version

Install Python dependencies in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install piecash "SQLAlchemy<2"
```

Depending on the version of piecash you use, you may need a different SQLAlchemy version. The importer suppresses known piecash/SQLAlchemy relationship warnings, but should still be tested with your installed environment.

## GnuCash File Format

`piecash` works with SQL-backed GnuCash books, not normal XML GnuCash books. The importer expects a SQLite `.gnucash` file.

To convert an XML book:

1. Open the XML book in GnuCash.
2. Choose **File → Save As…**.
3. Select the **sqlite3** format.
4. Save a new copy, for example:

```text
portfolio-sqlite.gnucash
```

Keep your original XML file as a backup.

## Safety First

Before every real import:

1. Close GnuCash and any other process using the SQLite book.
2. Make a copy of the `.gnucash` file.
3. Run the importer with `--dry-run` first.
4. Verify source account, dates, amounts, suggestions, and duplicate prompts.
5. Only then run without `--dry-run`.

A practical backup command on macOS/Linux is:

```bash
cp portfolio-sqlite.gnucash portfolio-sqlite.gnucash.backup-$(date +%Y%m%d-%H%M%S)
```

## Quick Start

Run an interactive dry-run against a Revolut CSV:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --dry-run
```

For a real import, omit `--dry-run`:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv"
```

The importer asks which GnuCash account the CSV belongs to. This is the **source account**: the bank account from which money left or arrived.

For example, a Revolut GBP import might use:

```text
Assets:Current Assets:Revolut GBP
```

An Alpha Bank Greece EUR import might use:

```text
Assets:Current Assets:Alpha Bank Savings
```

After selecting or confirming destination accounts, the script prepares transactions. It asks for final confirmation before saving them to the GnuCash book.

## Source Account Selection

Every imported transaction needs two sides:

1. The source bank account represented by the CSV.
2. The destination account selected for the payee, such as an expense, income, or transfer category.

For a card payment of £35.08 from Revolut to Toby Carvery, the resulting transaction is conceptually:

```text
Assets:Current Assets:Revolut GBP       -35.08 GBP
Expenses:Entertainment:Recreation:...   +35.08 GBP
```

The two split values sum to zero, satisfying GnuCash double-entry rules.

You can avoid the interactive source-account prompt with `--source-account`:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --source-account="Assets:Current Assets:Revolut GBP" \
  --dry-run
```

The selector includes GnuCash asset-like account types such as `ASSET`, `BANK`, `CASH`, `CHECKING`, `STOCK`, `MUTUAL`, and `RECEIVABLE`. It excludes internal GnuCash accounts such as:

- `Orphan-GBP`, `Orphan-EUR`, etc.
- Scheduled transaction template accounts, often shown with random GUID-like names and a `template` commodity

If the account you need is not visible, type a partial search term at the prompt:

```text
Enter number, exact account path (Tab to autocomplete), or search term: alpha
```

The script searches all eligible asset-like accounts and presents matching entries.

## Supported CSV Formats

### Revolut

The importer recognizes the modern Revolut export format:

```text
Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
```

Example:

```text
Card Payment,Current,2026-08-15 11:18:10,2026-08-16 11:16:55,Charing Cross Car Park,-3.50,0.00,GBP,COMPLETED,348.86
```

Mapping:

| Revolut column | Imported field |
|---|---|
| `Completed Date` | Transaction date; falls back to `Started Date` |
| `Description` | Payee / GnuCash description |
| `Amount` | Signed transaction amount |
| `Currency` | Currency code |
| `Type` + `State` | Memo |

Pending Revolut transactions are excluded by default. To include them:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --include-pending
```

### Bank of Scotland

The importer supports Bank of Scotland CSV exports with columns similar to:

```text
Date,Description,Type,Money In (£),Money Out (£),Balance (£)
```

Example logical row:

```text
03 Aug 26,DVLA-SB61XDW,DD,,35.93,1849.75
```

Mapping:

| Bank of Scotland column | Imported field |
|---|---|
| `Date` | Transaction date, parsed as `DD Mon YY` |
| `Description` | Payee |
| `Money In (£)` | Positive amount |
| `Money Out (£)` | Negative amount |
| `Type` | Human-readable memo, e.g. `DD` → `Direct Debit` |

Bank of Scotland PDF statements are **not** directly importable. Use the bank's online-banking transaction download/export function to obtain CSV data first.

### Alpha Bank Greece

Alpha Bank exports are detected automatically from their Greek header row and semicolon delimiter. They may include metadata before the actual transaction header:

```text
Τίτλος;Κινήσεις Λογαριασμού: ;IBAN;;;;
Ημ/νία;22/08/2026 09:16;;;;;;
...
Α/Α;Ημ/νία;Αιτιολογία;Κατάστημα;Τοκισμός από;Αρ. συναλλαγής;Ποσό;Πρόσημο ποσού
```

The importer skips the metadata preamble and starts at the real header.

| Greek column | Meaning | Imported field |
|---|---|---|
| `Ημ/νία` | Date | Transaction date, `DD/MM/YYYY` |
| `Αιτιολογία` | Reason/description | Payee |
| `Αρ. συναλλαγής` | Transaction reference | Memo |
| `Ποσό` | Amount | Amount parsed using European decimal format |
| `Πρόσημο ποσού` | Amount sign | `Χ` = debit/negative; `Π` = credit/positive |

Examples:

```text
135,10;Χ        -> -135.10 EUR
1.100,00;Π      -> +1100.00 EUR
1.532,76;Χ      -> -1532.76 EUR
```

Alpha Bank exports are treated as EUR transactions by default.

## Account Matching

The importer proposes destination accounts using several layers, in priority order:

1. Existing manually saved payee mappings.
2. Transaction history from the GnuCash book.
3. Exact and substring account-name matches.
4. Token/keyword overlap against account names.

For example, a payee such as:

```text
Charing Cross Car Park
```

can suggest accounts such as:

```text
Expenses:Auto:Parking:Quay car park
Expenses:Auto:Parking:Odyssey car park
```

because they share meaningful keywords such as `car` and `park`.

The importer also down-weights generic history words such as `transfer` and `payment`, making rarer terms such as a surname more useful for matching.

### Persistent Payee Mappings

When you select an account and agree to save it, the mapping is written to:

```text
.payee_account_mappings.json
```

On future imports, the same payee goes directly to that account if the currency matches.

List mappings:

```bash
python ./gnucash_importer.py --list-mappings
```

Clear mappings:

```bash
python ./gnucash_importer.py --clear-mappings
```

## Create a New Account

If no exact destination account exists, the importer can create one. It suggests a path based on the strongest existing category match.

For example, if the best account suggestions are below:

```text
Expenses:Auto:Parking:Quay car park
Expenses:Auto:Parking:Odyssey car park
```

then creating a new account for `Charing Cross Car Park` suggests:

```text
Expenses:Auto:Charing Cross Car Park
```

New accounts are saved to the GnuCash book immediately rather than waiting until transaction import is confirmed. This avoids a stale payee mapping pointing at an account that only existed in memory.

## Manual Account Entry

The account-selection menu contains:

```text
Enter account path manually (tab to autocomplete)
```

In compatible terminal environments, type the beginning of an account path and press **Tab** to complete it. Press Tab twice to show alternatives.

Examples:

```text
Assets:Current Assets:Re<Tab>
Expenses:Misc<Tab>
```

If you enter an existing full account path, it is selected. If you enter a new valid path, the importer creates the missing account path components.

## Skipping Transactions

The interactive account-selection menu has two different skip options:

```text
Skip and mark as imported (never ask again)
Skip for now (ask again next run)
```

### Skip and Mark as Imported

This does **not** create a GnuCash transaction. It writes a record to:

```text
.imported_transactions.json
```

with `"skipped": true`, using the transaction's content hash. On future imports, the importer reports:

```text
Already marked as skipped/imported - skipping
```

This is useful for items you intentionally do not want in GnuCash, such as transfers handled elsewhere, non-financial entries, duplicates, or known irrelevant transactions.

### Skip for Now

This skips only the current run. Nothing is recorded, so the row returns next time.

## Duplicate Detection

The importer uses **two independent duplicate checks**.

### 1. Importer History

The transaction hash is based on:

```text
CSV date | payee | amount
```

Any transaction already recorded in `.imported_transactions.json` is skipped automatically. This catches rows imported or deliberately skipped by the script before.

### 2. Existing GnuCash Ledger Check

Before processing rows, the importer builds an index of existing transactions already in the GnuCash book, restricted to the selected source account. It uses the key:

```text
(post_date, signed source-account split amount)
```

This catches transactions that exist in GnuCash but were not created by this importer, such as manually entered records or records imported through GnuCash's own bank-import workflow.

When a match is found, the importer asks:

```text
POSSIBLE DUPLICATE: an existing transaction on 2026-08-19 for the same amount is already in the ledger:
  'Existing description'
Is this the same transaction? [Y]es (skip) / n (import anyway):
```

- Enter or `Y`: mark the CSV row skipped/imported so it does not return.
- `n`: import it anyway.

Disable the ledger check when you know the file is entirely new data:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --no-ledger-check
```

## Dry-Run Mode

Dry-run mode opens the GnuCash book read-only and never writes transactions, accounts, mappings, or skipped-record history:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --dry-run
```

Dry-run mode still:

- Detects the bank format.
- Loads and analyzes the account tree.
- Selects the source account.
- Checks the existing ledger for likely duplicates.
- Displays suggestions and planned actions.

It does **not** open the manual account-selection menu, create accounts, save mappings, or mark skipped rows as imported.

## Auto-Accept Mode

`--auto-accept` takes the top suggestion automatically and performs the import without per-row prompts:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --source-account="Assets:Current Assets:Revolut GBP" \
  --auto-accept
```

Be cautious: automatic acceptance is suitable only after you have tested the matching quality using dry-runs and saved reliable payee mappings. If no account suggestion exists, the importer marks the row skipped/imported rather than guessing.

## Record Validation

Use the companion `validate_import.py` tool to compare previously imported transactions with the source CSV and identify post-date mismatches:

```bash
python validate_import.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv"
```

To correct detected date mismatches:

```bash
python validate_import.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="transactions.csv" \
  --fix
```

Transactions imported before transaction GUID tracking was added may be reported as legacy imports that cannot be automatically verified. Rows deliberately skipped-and-marked-as-imported are reported separately and not treated as errors.

## Export Accounts to JSON

Export the full account tree to JSON:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --export-accounts
```

The generated `accounts.json` includes each account's GUID, name, fullname, type, description, currency, and parent GUID.

## Stale SQLite Locks

GnuCash SQLite books use an internal `gnclock` table. If GnuCash, piecash, or the importer crashes before closing the book, a stale lock may remain.

The importer uses `try/finally` to call `book.close()` on normal completion, errors, and Ctrl+C interruptions. It prints:

```text
GnuCash book closed - lock released.
```

If a stale lock remains from an earlier crash, close GnuCash and every importer process first, then run the companion cleanup tool:

```bash
python clear_gnclock.py portfolio-sqlite.gnucash
```

Only do this when no process genuinely has the file open. Clearing an active lock while another writer is using the database risks corruption.

## Typical Commands

### Revolut import, review first

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

After reviewing output, run the same command without `--dry-run` to perform the import.

## Persistent Files

The script keeps small JSON state files in its current working directory:

| File | Purpose |
|---|---|
| `.imported_transactions.json` | Tracks imported and permanently skipped rows using a content hash |
| `.payee_account_mappings.json` | Stores payee → GnuCash account mappings |
| `.transaction_history_analysis.json` | Cached analysis of existing transaction history |
| `accounts.json` | Optional exported account tree |

Keep these files in the same project directory as the importer if you want mappings and duplicate history to persist consistently.

## Troubleshooting

### `Invalid argument(s) 'backend' sent to create_engine()`

Remove obsolete `backend="xml"` or `check_exists=False` arguments from `piecash.open_book()`. The current script opens SQLite books directly and does not use an XML backend option.

### `CSV not found`

Pass the real CSV path explicitly:

```bash
python ./gnucash_importer.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --csv-file="/full/path/to/statement.csv"
```

### `GncImbalanceError`

This means a transaction did not have balanced splits. The current script creates one source-account split and one equal-and-opposite destination-account split, so this should not occur unless the script has been modified or the source account is invalid.

### Account not listed

At the source-account prompt, type a partial name, for example:

```text
alpha
revolut
tsb
bos
```

The script searches all asset-like account types, including GnuCash `BANK` accounts, not only generic `ASSET` accounts.

### `Orphan-GBP` or GUID/template entries appear

Use the latest version of the script. It excludes:

- `Orphan-<currency>` accounts, which are GnuCash unresolved-split/suspense artifacts.
- Template accounts used internally by scheduled transactions, typically named with a GUID-like hex string and commodity `template`.

## Limitations

- The duplicate ledger check deliberately uses exact **date + amount** on the selected source account, because this is reliable and avoids broad false positives. It may still flag legitimate same-day transactions with the same amount; the interactive prompt lets you import anyway.
- Matching suggestions are advisory. Review new or low-confidence mappings before accepting them.
- Do not use the same SQLite GnuCash book simultaneously in GnuCash desktop and the importer.
- Make backups before any non-dry-run import.
- The script does not parse PDF statements directly; obtain CSV exports from banks where necessary.
