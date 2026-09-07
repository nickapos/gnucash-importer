"""Shared importer configuration."""

DEFAULT_GNUCASH_FILE = "portfolio-sqlite.gnucash"
DEFAULT_CSV_FILE = "transactions.csv"
BASE_CURRENCY = "GBP"
MAX_ACCOUNT_SUGGESTIONS = 5
DEFAULT_KEEP_BACKUPS = 10
DEFAULT_PENDING_DUPLICATE_WINDOW_DAYS = 7

DUPLICATE_CHECK_FILE = ".imported_transactions.json"
MAPPINGS_FILE = ".payee_account_mappings.json"
TRANSACTION_HISTORY_FILE = ".transaction_history_analysis.json"
ACCOUNTS_EXPORT_FILE = "accounts.json"

SUPPORTED_CURRENCIES = {"GBP", "USD", "EUR"}

# These are account types that may legitimately be the source of a bank or
# card statement import. They include both asset-side accounts (bank/current/
# savings/cash) and liability-side accounts (credit cards and loans). GnuCash
# represents credit cards using the distinct CREDIT type, so omitting CREDIT
# prevents a card account from ever appearing in source selection.
SOURCE_ACCOUNT_TYPES = {
    # Asset-side account types.
    "ASSET",
    "BANK",
    "CASH",
    "CHECKING",
    "STOCK",
    "MUTUAL",
    "RECEIVABLE",

    # Liability-side statement account types.
    "CREDIT",
    "LIABILITY",
}

# Compatibility alias for any older module that still imports this name.
# New code should import SOURCE_ACCOUNT_TYPES instead.
ASSET_LIKE_TYPES = SOURCE_ACCOUNT_TYPES

BOS_TYPE_DESCRIPTIONS = {
    "BGC": "Bank Giro Credit",
    "BP": "Bill Payment",
    "CHG": "Charge",
    "CHQ": "Cheque",
    "COR": "Correction",
    "CPT": "Cashpoint",
    "DD": "Direct Debit",
    "DEB": "Debit Card",
    "DEP": "Deposit",
    "FEE": "Fixed Service Fee",
    "FPI": "Faster Payment In",
    "FPO": "Faster Payment Out",
    "MPI": "Mobile Payment In",
    "MPO": "Mobile Payment Out",
    "PAY": "Payment",
    "SO": "Standing Order",
    "TFR": "Transfer",
}

# Alpha Bank Greece account export header marker. Alpha CSV exports have a
# metadata preamble before this header row.
ALPHA_GR_HEADER_MARKER = "Α/Α"
