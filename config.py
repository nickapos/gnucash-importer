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
ASSET_LIKE_TYPES = {
    "ASSET",
    "BANK",
    "CASH",
    "CHECKING",
    "STOCK",
    "MUTUAL",
    "RECEIVABLE",
}

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

ALPHA_GR_HEADER_MARKER = "Α/Α"
