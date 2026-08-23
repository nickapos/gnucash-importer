#!/usr/bin/env python3
"""
GnuCash CSV Importer with Transaction History Analysis and Currency Support
==========================================================================
Automatically learns from existing transactions to suggest mappings.
Supports GBP (default), USD, and EUR currencies for accounts.

Works against a SQLite-backed GnuCash book. Every write run creates one
copy-on-write timestamped database backup before the first account or
transaction modification, then retains a configurable number of recent
backups.
"""

import csv
import json
import os
import re
import shutil
import warnings
import piecash
import hashlib
from decimal import Decimal
from datetime import datetime
from collections import defaultdict, Counter
from typing import Dict, List, Optional, Tuple, Set
import difflib

from sqlalchemy import exc as sa_exc

warnings.filterwarnings("ignore", category=sa_exc.SAWarning)

DEFAULT_GNUCASH_FILE = "portfolio-sqlite.gnucash"
DEFAULT_CSV_FILE = "transactions.csv"
BASE_CURRENCY = "GBP"
MAX_ACCOUNT_SUGGESTIONS = 5
DEFAULT_KEEP_BACKUPS = 10
DUPLICATE_CHECK_FILE = ".imported_transactions.json"
MAPPINGS_FILE = ".payee_account_mappings.json"
TRANSACTION_HISTORY_FILE = ".transaction_history_analysis.json"
ACCOUNTS_EXPORT_FILE = "accounts.json"
SUPPORTED_CURRENCIES = {"GBP", "USD", "EUR"}

ASSET_LIKE_TYPES = {
    "ASSET", "BANK", "CASH", "CHECKING", "STOCK", "MUTUAL", "RECEIVABLE"
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


def _is_real_asset_account(acct) -> bool:
    """Exclude GnuCash template and orphan bookkeeping accounts."""
    commod = getattr(acct, "commodity", None)
    if commod is not None and getattr(commod, "namespace", "") == "template":
        return False
    if acct.fullname.split(":")[-1].startswith("Orphan-"):
        return False
    return True


def parse_tx_date(date_str: str) -> datetime:
    """Parse a source date using supported bank export formats."""
    date_str = (date_str or "").strip()
    if not date_str:
        print("  Warning: empty date field - using current date/time as fallback")
        return datetime.now()

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d %b %y",
        "%d %b %Y",
        "%d-%b-%y",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
        "%d-%m-%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    print(f"  Warning: could not parse date '{date_str}' - using current date/time as fallback")
    return datetime.now()


class AccountPathCompleter:
    def __init__(self, account_fullnames: List[str]):
        self.account_fullnames = sorted(set(account_fullnames))

    def complete(self, text: str, state: int) -> Optional[str]:
        if not text:
            matches = self.account_fullnames
        else:
            matches = [
                a for a in self.account_fullnames if a.lower().startswith(text.lower())
            ]
        try:
            return matches[state]
        except IndexError:
            return None


def _input_with_completion(prompt: str, completer: "AccountPathCompleter") -> str:
    try:
        import readline

        old_completer = readline.get_completer()
        old_delims = readline.get_completer_delims()
        readline.set_completer(completer.complete)
        readline.set_completer_delims("")
        readline.parse_and_bind("tab: complete")
        try:
            return input(prompt)
        finally:
            readline.set_completer(old_completer)
            readline.set_completer_delims(old_delims)
    except (ImportError, AttributeError):
        return input(prompt)


class TransactionHistoryAnalyzer:
    def __init__(self, book: piecash.Book):
        self.book = book
        self.payee_to_accounts = defaultdict(list)
        self.word_patterns = defaultdict(Counter)
        self.analysis_cache = self._load_analysis()

    def _load_analysis(self) -> dict:
        if os.path.exists(TRANSACTION_HISTORY_FILE):
            try:
                with open(TRANSACTION_HISTORY_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_analysis(self):
        try:
            with open(TRANSACTION_HISTORY_FILE, "w") as f:
                json.dump(self.analysis_cache, f, indent=2)
        except Exception as e:
            print(f"Could not save analysis: {e}")

    def analyze_transactions(self, max_transactions: int = 1000):
        print("Analyzing transaction history...")
        txns = list(self.book.transactions)
        txns.sort(key=lambda t: t.post_date or t.enter_date, reverse=True)
        txns = txns[:max_transactions]
        payee_usage = defaultdict(Counter)

        for txn in txns:
            if not txn.splits:
                continue
            payee = (txn.description or "").strip()
            if not payee:
                continue
            for split in txn.splits:
                if split.account:
                    account_full = split.account.fullname
                    payee_usage[payee][account_full] += 1
                    for word in self._extract_words(payee):
                        self.word_patterns[word][account_full] += 1

        self.payee_to_accounts = defaultdict(list)
        for payee, accounts in payee_usage.items():
            top_count = max(accounts.values())
            for account, count in accounts.most_common(5):
                self.payee_to_accounts[payee].append(
                    {
                        "account": account,
                        "count": count,
                        "confidence": min(count / top_count, 1.0),
                    }
                )

        self.analysis_cache = {
            "last_analyzed": datetime.now().isoformat(),
            "transactions_analyzed": len(txns),
            "payee_mappings": self.payee_to_accounts,
            "word_patterns": {
                word: dict(counts.most_common(10))
                for word, counts in self.word_patterns.items()
            },
        }
        self._save_analysis()
        print(
            f"Analyzed {len(txns)} transactions, found {len(self.payee_to_accounts)} unique payees"
        )

    @staticmethod
    def _extract_words(text: str) -> List[str]:
        text = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
        words = text.split()
        stop = {
            "the", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by",
        }
        return [word for word in words if len(word) > 2 and word not in stop]

    def suggest_mapping(
        self, csv_payee: str, csv_description: str = ""
    ) -> List[Tuple[str, float, str]]:
        suggestions = []
        payee_l = csv_payee.lower().strip()
        desc_l = csv_description.lower().strip()

        if csv_payee in self.payee_to_accounts:
            for mapping in self.payee_to_accounts[csv_payee]:
                suggestions.append(
                    (
                        mapping["account"],
                        mapping["confidence"],
                        f"Exact match: '{csv_payee}' used {mapping['count']} times",
                    )
                )

        for payee, mappings in self.payee_to_accounts.items():
            similarity = self._similarity(payee_l, payee.lower())
            if similarity > 0.7:
                for mapping in mappings:
                    suggestions.append(
                        (
                            mapping["account"],
                            mapping["confidence"] * similarity,
                            f"Fuzzy match: '{payee}' (similarity {similarity:.1%})",
                        )
                    )

        words = set(self._extract_words(csv_payee + " " + csv_description))
        word_scores = defaultdict(float)
        for word in words:
            accounts_for_word = self.word_patterns.get(word, {})
            if not accounts_for_word:
                continue
            rarity_weight = 1.0 / len(accounts_for_word)
            for account, count in accounts_for_word.items():
                word_scores[account] += count * rarity_weight

        matched_words = sorted(
            (word for word in words if word in self.word_patterns),
            key=lambda word: len(self.word_patterns.get(word, {})),
        )
        for account, score in Counter(word_scores).most_common(5):
            if score > 0.15:
                word_hint = matched_words[0] if matched_words else ""
                suggestions.append(
                    (
                        account,
                        min(score, 0.9),
                        f"Word pattern: '{word_hint}' appears in similar transactions",
                    )
                )

        if csv_description:
            for payee, mappings in self.payee_to_accounts.items():
                if desc_l in payee.lower() or payee.lower() in desc_l:
                    for mapping in mappings:
                        suggestions.append(
                            (
                                mapping["account"],
                                mapping["confidence"] * 0.9,
                                f"Description match: '{csv_description}' relates to '{payee}'",
                            )
                        )

        best = {}
        for account, confidence, reason in suggestions:
            if account not in best or confidence > best[account][0]:
                best[account] = (confidence, reason)
        return [(account, confidence, reason) for account, (confidence, reason) in best.items()]

    @staticmethod
    def _similarity(s1: str, s2: str) -> float:
        return difflib.SequenceMatcher(None, s1, s2).ratio()


class PayeeAccountMapper:
    def __init__(self, mappings_file: str = MAPPINGS_FILE):
        self.mappings_file = mappings_file
        self.mappings = self._load_mappings()

    def _load_mappings(self) -> Dict:
        if os.path.exists(self.mappings_file):
            try:
                with open(self.mappings_file, "r") as f:
                    data = json.load(f)
                print(f"Loaded {len(data)} payee mappings from {self.mappings_file}")
                return data
            except Exception as e:
                print(f"Could not load mappings: {e}")
        return {}

    def _save_mappings(self):
        with open(self.mappings_file, "w") as f:
            json.dump(self.mappings, f, indent=2, sort_keys=True)

    def get_mapping(self, payee: str) -> Optional[Dict]:
        if payee in self.mappings:
            return self.mappings[payee]
        payee_l = payee.lower().strip()
        for stored_payee, mapping in self.mappings.items():
            stored_l = stored_payee.lower().strip()
            if stored_l in payee_l or payee_l in stored_l:
                if len(stored_l) >= 3 and len(payee_l) >= 3:
                    return mapping
        return None

    def add_mapping(self, payee: str, account_fullname: str, account_guid: str):
        self.mappings[payee] = {
            "account_fullname": account_fullname,
            "account_guid": account_guid,
            "created": datetime.now().isoformat(),
            "last_used": datetime.now().isoformat(),
            "use_count": self.mappings.get(payee, {}).get("use_count", 0) + 1,
        }
        self._save_mappings()

    def update_last_used(self, payee: str):
        if payee in self.mappings:
            self.mappings[payee]["last_used"] = datetime.now().isoformat()
            self.mappings[payee]["use_count"] = self.mappings[payee].get("use_count", 0) + 1
            self._save_mappings()

    def remove_mapping(self, payee: str) -> bool:
        if payee in self.mappings:
            del self.mappings[payee]
            self._save_mappings()
            return True
        return False

    def list_mappings(self) -> List[Tuple[str, Dict]]:
        return sorted(
            self.mappings.items(),
            key=lambda item: item[1].get("use_count", 0),
            reverse=True,
        )


class BankFormatMapper:
    @staticmethod
    def map_revolut(row: dict) -> dict:
        date_str = (
            row.get("Completed Date")
            or row.get("Started Date")
            or row.get("date")
            or row.get("transaction_date", "")
        )
        payee = (
            row.get("Description")
            or row.get("counterparty")
            or row.get("description")
            or row.get("merchant_name", "")
        )
        raw_amount = row.get("Amount", row.get("amount", "0"))
        try:
            amount = float(str(raw_amount).replace("£", "").replace(",", "."))
        except ValueError:
            amount = 0.0
        currency = (row.get("Currency") or row.get("currency", "")).upper() or BASE_CURRENCY
        state = row.get("State", "")
        memo_parts = [part for part in [row.get("Type", ""), state] if part]
        return {
            "date": date_str,
            "payee": payee,
            "amount": amount,
            "currency": currency,
            "memo": " / ".join(memo_parts) if memo_parts else row.get("notes", ""),
            "state": state,
            "original_row": row,
        }

    @staticmethod
    def map_bof_scot(row: dict) -> dict:
        date_str = row.get("Date") or row.get("Posting Date") or row.get("Value Date", "")
        payee = row.get("Description") or row.get("Payee", "")
        type_code = (row.get("Type") or "").strip().upper()

        def clean_amount(raw: str) -> float:
            raw = (raw or "").strip()
            if not raw or raw.lower() == "blank":
                return 0.0
            try:
                return float(raw.replace("£", "").replace(",", ""))
            except ValueError:
                return 0.0

        money_in = clean_amount(row.get("Money In (£)") or row.get("Money In") or row.get("Credit"))
        money_out = clean_amount(row.get("Money Out (£)") or row.get("Money Out") or row.get("Debit"))
        type_desc = BOS_TYPE_DESCRIPTIONS.get(type_code, type_code)
        return {
            "date": date_str,
            "payee": payee,
            "amount": money_in - money_out,
            "currency": BASE_CURRENCY,
            "memo": type_desc or row.get("Reference") or row.get("Notes", ""),
            "original_row": row,
        }

    @staticmethod
    def map_alpha_gr(row: dict) -> dict:
        date_str = row.get("Ημ/νία") or ""
        payee = row.get("Αιτιολογία") or ""
        raw_amount = row.get("Ποσό") or "0"
        sign_code = (row.get("Πρόσημο ποσού") or "").strip().upper()
        reference = row.get("Αρ. συναλλαγής") or ""

        cleaned = raw_amount.strip().replace(".", "").replace(",", ".")
        try:
            amount = float(cleaned)
        except ValueError:
            amount = 0.0

        if sign_code == "Χ":
            amount = -abs(amount)
        elif sign_code == "Π":
            amount = abs(amount)

        return {
            "date": date_str,
            "payee": payee,
            "amount": amount,
            "currency": "EUR",
            "memo": f"Ref: {reference}" if reference else "",
            "original_row": row,
        }

    @staticmethod
    def detect_format(csv_file: str) -> str:
        try:
            with open(csv_file, "r", encoding="utf-8-sig", newline="") as f:
                for _ in range(10):
                    line = f.readline()
                    if not line:
                        break
                    if line.strip().startswith(ALPHA_GR_HEADER_MARKER):
                        return "alpha_gr"

                f.seek(0)
                header = next(csv.reader(f))

            lower_header = [h.strip().lower() for h in header]
            original_header = [h.strip() for h in header]

            revolut_signals = ["started date", "completed date", "product", "state"]
            if any("counterparty" in h for h in lower_header) or sum(
                signal in lower_header for signal in revolut_signals
            ) >= 2:
                return "revolut"

            bof_signals = ["money in", "money out", "balance"]
            if any("posting date" == h.lower() for h in original_header) or any(
                "posting date" in h for h in lower_header
            ):
                return "bof_scot"
            if sum(any(signal in h for signal in bof_signals) for h in lower_header) >= 2 and any(
                "type" in h for h in lower_header
            ):
                return "bof_scot"

            required = ["date", "payee", "amount"]
            if all(any(field in h for h in lower_header) for field in required):
                return "generic"
        except Exception as e:
            print(f"Warning: Could not detect format: {e}")
        return "generic"

    @staticmethod
    def map_generic(row: dict) -> dict:
        lower_row = {key.lower().strip(): value for key, value in row.items()}
        try:
            amount = float(
                str(lower_row.get("amount", "0")).replace("£", "").replace(",", ".").strip()
            )
        except (ValueError, AttributeError):
            amount = 0.0
        return {
            "date": lower_row.get("date", ""),
            "payee": lower_row.get("payee", ""),
            "amount": amount,
            "currency": lower_row.get("currency", "").upper() or BASE_CURRENCY,
            "memo": lower_row.get("memo", ""),
            "original_row": row,
        }


class AccountMatcher:
    def __init__(self, book, payee_mapper, history_analyzer):
        self.book = book
        self.payee_mapper = payee_mapper
        self.history_analyzer = history_analyzer
        self.accounts_cache = {}
        self._cache_accounts()

    def _cache_accounts(self):
        for account in self.book.accounts:
            self.accounts_cache[account.guid] = account

    @staticmethod
    def _acct_currency_matches(acct, code: str) -> bool:
        commodity = getattr(acct, "commodity", None)
        return bool(
            commodity
            and commodity.namespace == "CURRENCY"
            and commodity.mnemonic.upper() == code.upper()
        )

    @staticmethod
    def _extract_tokens(text: str) -> Set[str]:
        text = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
        stop = {"the", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by"}
        return {word for word in text.split() if len(word) > 2 and word not in stop}

    def _keyword_account_matches(self, payee: str, transaction_currency: str):
        payee_tokens = self._extract_tokens(payee)
        if not payee_tokens:
            return []
        matches = []
        for account in self.accounts_cache.values():
            if not self._acct_currency_matches(account, transaction_currency):
                continue
            account_tokens = self._extract_tokens(account.fullname.replace(":", " "))
            overlap = payee_tokens & account_tokens
            if not overlap:
                continue
            score = len(overlap) / len(payee_tokens)
            if any(len(word) >= 5 for word in overlap):
                score = min(score + 0.15, 0.9)
            matches.append(
                (account, round(score, 2), f"Keyword match: shared word(s) {', '.join(sorted(overlap))!r}")
            )
        return sorted(matches, key=lambda item: item[1], reverse=True)

    def find_matching_accounts(self, payee, description="", transaction_currency=BASE_CURRENCY,
                               max_suggestions=MAX_ACCOUNT_SUGGESTIONS, exclude_guid=None):
        suggestions = []
        mapping = self.payee_mapper.get_mapping(payee)
        if mapping:
            account = self.get_account_by_guid(mapping["account_guid"])
            if account and self._acct_currency_matches(account, transaction_currency):
                suggestions.append((account, 1.0, f"Manual mapping (used {mapping.get('use_count', 1)} times)"))

        for account_fullname, confidence, reason in self.history_analyzer.suggest_mapping(payee, description):
            account = self._find_account_by_fullname(account_fullname)
            if account and self._acct_currency_matches(account, transaction_currency):
                suggestions.append((account, confidence, f"History: {reason}"))

        payee_l = payee.lower().strip()
        for account in self.accounts_cache.values():
            if not self._acct_currency_matches(account, transaction_currency):
                continue
            name_l = account.name.lower().strip()
            fullname_l = account.fullname.lower().strip()
            if payee_l == name_l or payee_l == fullname_l:
                suggestions.append((account, 0.95, "Exact account name match"))
            elif payee_l in name_l or payee_l in fullname_l:
                suggestions.append((account, 0.8, "Account name contains payee"))

        suggestions.extend(self._keyword_account_matches(payee, transaction_currency))
        if exclude_guid:
            suggestions = [item for item in suggestions if item[0].guid != exclude_guid]

        best = {}
        for account, confidence, reason in suggestions:
            if account.guid not in best or confidence > best[account.guid][0]:
                best[account.guid] = (confidence, reason, account)
        return sorted(
            [(account, confidence, reason) for account, (confidence, reason, account) in best.items()],
            key=lambda item: item[1], reverse=True,
        )[:max_suggestions]

    def _find_account_by_fullname(self, fullname):
        for account in self.accounts_cache.values():
            if account.fullname == fullname:
                return account
        return None

    @staticmethod
    def suggest_category(payee):
        text = payee.lower()
        if any(word in text for word in ["tesco", "sainsbury", "asda", "supermarket", "food", "waitrose", "aldi", "lidl"]):
            return "Expenses:Food"
        if any(word in text for word in ["amazon", "ebay", "shop", "retail", "asos", "next", "argos"]):
            return "Expenses:Shopping"
        if any(word in text for word in ["shell", "bp", "esso", "petrol", "fuel", "transport", "uber", "train", "bus", "car park", "parking"]):
            return "Expenses:Transportation"
        if any(word in text for word in ["netflix", "spotify", "subscription", "monthly", "disney", "prime"]):
            return "Expenses:Subscriptions"
        if any(word in text for word in ["salary", "wages", "income", "payment", "payroll", "hmrc", "tax"]):
            return "Income:Salary"
        if any(word in text for word in ["electric", "gas", "water", "broadband", "internet", "phone", "utilities", "council"]):
            return "Expenses:Utilities"
        if any(word in text for word in ["vodafone", "cosmote", "wind", "nova"]):
            return "Expenses:Phone"
        return "Expenses:Miscellaneous"

    @staticmethod
    def _suggest_new_account_path(payee, suggestions, fallback_category):
        if suggestions:
            parts = suggestions[0][0].fullname.split(":")
            if len(parts) >= 2:
                return f"{':'.join(parts[:2])}:{payee}"
        return f"{fallback_category}:{payee}"

    def get_account_by_guid(self, guid):
        return self.accounts_cache.get(guid)

    def get_account_guid(self, name):
        for account in self.accounts_cache.values():
            if account.name == name or account.fullname == name:
                return account.guid
        return None


class TransactionImporter:
    def __init__(self, gnucash_file, csv_file):
        self.gnucash_file = gnucash_file
        self.csv_file = csv_file
        self.book = None
        self.mapper = BankFormatMapper()
        self.payee_mapper = PayeeAccountMapper()
        self.history_analyzer = None
        self.matcher = None
        self.imported_tx = self._load_imported()
        self.dry_run = False
        self.auto_accept = False
        self.source_account = None
        self.tx_to_create = []
        self._existing_ledger_index = None
        self.check_ledger = True

    def _load_imported(self):
        if os.path.exists(DUPLICATE_CHECK_FILE):
            try:
                with open(DUPLICATE_CHECK_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_imported(self):
        with open(DUPLICATE_CHECK_FILE, "w") as f:
            json.dump(self.imported_tx, f, indent=2)

    def _tx_hash(self, tx):
        return hashlib.md5(f"{tx['date']}|{tx['payee']}|{tx['amount']:.2f}".encode("utf-8")).hexdigest()

    def _mark_skipped(self, tx, reason="user skipped"):
        self.imported_tx[tx["import_hash"]] = {
            "timestamp": datetime.now().isoformat(),
            "payee": tx["payee"],
            "amount": f"{tx['amount']:.2f}",
            "currency": tx["currency"],
            "csv_date": tx["date"],
            "skipped": True,
            "reason": reason,
        }
        self._save_imported()
        print(f"  Marked as imported (skipped): '{tx['payee']}' will not be shown again")

    def _build_existing_ledger_index(self):
        index = defaultdict(list)
        for txn in self.book.transactions:
            for split in txn.splits or []:
                if split.account is not None and split.account.guid == self.source_account.guid:
                    index[(txn.post_date, round(float(split.value), 2))].append(txn)
        self._existing_ledger_index = index
        print(f"Indexed {sum(len(v) for v in index.values())} existing ledger entries on {self.source_account.fullname} for duplicate detection")

    def _find_existing_ledger_match(self, tx):
        if not self._existing_ledger_index:
            return None
        key = (parse_tx_date(tx["date"]).date(), round(float(tx["amount"]), 2))
        candidates = self._existing_ledger_index.get(key, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        return max(candidates, key=lambda item: difflib.SequenceMatcher(None, tx["payee"].lower(), (item.description or "").lower()).ratio())

    def _get_or_create_commodity(self, code):
        code = code.upper()
        if code not in SUPPORTED_CURRENCIES:
            code = BASE_CURRENCY
        for commodity in self.book.commodities:
            if commodity.namespace == "CURRENCY" and commodity.mnemonic == code:
                return commodity
        new_commodity = piecash.Commodity(name=code, namespace="CURRENCY", mnemonic=code, fullname=f"{code} Currency", quote_source="Manual")
        self.book.commodities.append(new_commodity)
        return new_commodity

    def open_book(self, readonly=True):
        self.gnucash_file = os.path.abspath(self.gnucash_file)
        if not os.path.exists(self.gnucash_file):
            raise FileNotFoundError(f"GnuCash file not found: {self.gnucash_file}")
        self.book = piecash.open_book(self.gnucash_file, readonly=readonly, open_if_lock=True)

    def resolve_source_account(self, path_hint=None):
        if path_hint:
            guid = self.matcher.get_account_guid(path_hint)
            if guid:
                account = self.matcher.get_account_by_guid(guid)
                print(f"Using source account: {account.fullname}")
                return account
            print(f"Warning: account '{path_hint}' not found - please select manually.")

        all_accounts = [
            account for account in self.matcher.accounts_cache.values()
            if account.type in ASSET_LIKE_TYPES and _is_real_asset_account(account)
        ]
        postable = sorted((account for account in all_accounts if account.placeholder == 0), key=lambda account: account.fullname)
        print(f"\nFound {len(all_accounts)} asset-like accounts total ({len(postable)} postable, {len(all_accounts)-len(postable)} placeholder/organizational). (Orphan and Scheduled-Transaction template accounts are excluded.)")
        print("Which account does this CSV export belong to?")
        for i, account in enumerate(postable, 1):
            print(f"  {i}. {account.fullname} ({account.commodity.mnemonic if account.commodity else '?'})")
        print("  Type a search term (e.g. 'alpha') to filter/search ALL asset-like accounts, including placeholders, if needed.")

        completer = AccountPathCompleter([account.fullname for account in postable])
        while True:
            choice = _input_with_completion("Enter number, exact account path (Tab to autocomplete), or search term: ", completer).strip()
            if choice.isdigit() and 1 <= int(choice) <= len(postable):
                return postable[int(choice)-1]
            guid = self.matcher.get_account_guid(choice)
            if guid:
                account = self.matcher.get_account_by_guid(guid)
                if account.placeholder == 0:
                    return account
                print(f"  '{account.fullname}' is a placeholder and cannot directly hold transactions.")
                continue
            matches = [account for account in all_accounts if choice.lower() in account.fullname.lower()]
            if not matches:
                print(f"  No asset-like accounts found matching '{choice}' - try again.")
                continue
            print(f"\n  Found {len(matches)} account(s) matching '{choice}':")
            for i, account in enumerate(matches, 1):
                marker = " (placeholder - cannot hold transactions directly)" if account.placeholder != 0 else ""
                print(f"    {i}. {account.fullname} [{account.type}] ({account.commodity.mnemonic if account.commodity else '?'}){marker}")
            sub_choice = input("  Enter number to select, or press Enter to search again: ").strip()
            if sub_choice.isdigit() and 1 <= int(sub_choice) <= len(matches):
                account = matches[int(sub_choice)-1]
                if account.placeholder == 0:
                    return account
                print(f"  '{account.fullname}' is a placeholder and cannot be used directly.")

    def export_accounts_json(self, json_path=ACCOUNTS_EXPORT_FILE):
        accounts = {
            account.guid: {
                "guid": account.guid,
                "name": account.name,
                "fullname": account.fullname,
                "type": account.type,
                "description": account.description or "",
                "currency": account.commodity.mnemonic if account.commodity else None,
                "parent_guid": account.parent.guid if account.parent else None,
            }
            for account in self.book.accounts
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, sort_keys=True)
        print(f"Exported {len(accounts)} accounts to {json_path}")

    def read_csv(self, skip_pending=True):
        if not os.path.exists(self.csv_file):
            raise FileNotFoundError(f"CSV not found: {self.csv_file}")
        fmt = self.mapper.detect_format(self.csv_file)
        print(f"Detected bank format: {fmt}")
        delimiter = ";" if fmt == "alpha_gr" else ","
        with open(self.csv_file, "r", encoding="utf-8-sig", newline="") as f:
            lines = f.readlines()
        start_idx = 0
        if fmt == "alpha_gr":
            for i, line in enumerate(lines):
                if line.strip().startswith(ALPHA_GR_HEADER_MARKER):
                    start_idx = i
                    break
        rows = []
        for i, row in enumerate(csv.DictReader(lines[start_idx:], delimiter=delimiter), 1):
            if not any(str(value).strip() for value in row.values()):
                continue
            if fmt == "revolut":
                mapped = self.mapper.map_revolut(row)
            elif fmt == "bof_scot":
                mapped = self.mapper.map_bof_scot(row)
            elif fmt == "alpha_gr":
                mapped = self.mapper.map_alpha_gr(row)
            else:
                mapped = self.mapper.map_generic(row)
            if skip_pending and str(mapped.get("state", "")).upper() == "PENDING":
                continue
            mapped["row_number"] = i
            mapped["import_hash"] = self._tx_hash(mapped)
            rows.append(mapped)
        print(f"Read {len(rows)} transactions")
        return rows

    def process_transactions(self, transactions):
        print("\n" + "=" * 70)
        print("TRANSACTION PROCESSING")
        print("=" * 70)
        print(f"Source account: {self.source_account.fullname}")
        if self.check_ledger and self._existing_ledger_index is None:
            self._build_existing_ledger_index()

        for index, tx in enumerate(transactions, 1):
            print(f"\n[{index}/{len(transactions)}] Row {tx['row_number']}")
            print(f"  Date: {tx['date']}")
            print(f"  Payee: {tx['payee']}")
            print(f"  Amount: {abs(tx['amount']):.2f} {tx['currency']} {'(Expense)' if tx['amount'] <= 0 else '(Income)'}")
            if tx.get("memo"):
                print(f"  Memo: {tx['memo']}")
            if tx["import_hash"] in self.imported_tx:
                print("  Already marked as skipped/imported - skipping" if self.imported_tx[tx["import_hash"]].get("skipped") else "  Already imported - skipping")
                continue
            if self.check_ledger:
                match = self._find_existing_ledger_match(tx)
                if match is not None:
                    print(f"\n  POSSIBLE DUPLICATE: existing transaction on {match.post_date} with same amount: '{match.description}'")
                    if self.dry_run:
                        print("  [DRY RUN] Would flag as possible duplicate - skipping")
                        continue
                    if self.auto_accept:
                        self._mark_skipped(tx, "matched existing ledger entry (auto-accept)")
                        continue
                    answer = input("  Is this the same transaction? [Y]es (skip) / n (import anyway): ").strip().lower()
                    if answer in ["", "y", "yes"]:
                        self._mark_skipped(tx, "matched existing ledger entry (user confirmed)")
                        continue
            mapping = self.payee_mapper.get_mapping(tx["payee"])
            if mapping:
                account = self.matcher.get_account_by_guid(mapping["account_guid"])
                if account and self.matcher._acct_currency_matches(account, tx["currency"]):
                    print(f"\n  MAPPED: {mapping['account_fullname']} (used {mapping.get('use_count', 1)} times)")
                    if self.dry_run:
                        self._prepare_tx(account, tx)
                        continue
                    if self.auto_accept:
                        self.payee_mapper.update_last_used(tx["payee"])
                        self._prepare_tx(account, tx)
                        continue
                    answer = input("  Use this mapping? [Y]/n/edit: ").strip().lower()
                    if answer in ["", "y", "yes"]:
                        self.payee_mapper.update_last_used(tx["payee"])
                        self._prepare_tx(account, tx)
                        continue
                elif account is None:
                    self.payee_mapper.remove_mapping(tx["payee"])
            suggestions = self.matcher.find_matching_accounts(tx["payee"], tx.get("memo", ""), tx["currency"], exclude_guid=self.source_account.guid)
            if suggestions:
                print("\n  Suggested accounts (filtered by currency):")
                for i, (account, confidence, reason) in enumerate(suggestions, 1):
                    print(f"   {i}. {account.fullname} [{confidence:.0%}] {reason}")
            else:
                print(f"\n  No historical matches. Suggested category: {self.matcher.suggest_category(tx['payee'])}")
            if self.dry_run:
                print("  [DRY RUN] Skipping manual selection")
                continue
            if self.auto_accept:
                if suggestions:
                    self._prepare_tx(suggestions[0][0], tx)
                else:
                    self._mark_skipped(tx, "auto-accept: no suggestions available")
                continue
            selected = self._manual_sel(tx, suggestions)
            if selected:
                self._prepare_tx(selected, tx)

    def _manual_sel(self, tx, suggestions):
        print("\n  Account Selection:")
        for i, (account, confidence, _) in enumerate(suggestions, 1):
            print(f"   {i}. {account.fullname} ({confidence:.0%} confidence)")
        fallback = self.matcher.suggest_category(tx["payee"])
        suggested_path = self.matcher._suggest_new_account_path(tx["payee"], suggestions, fallback)
        print(f"   {len(suggestions)+1}. Create new account (suggested: {suggested_path})")
        print(f"   {len(suggestions)+2}. Enter account path manually (tab to autocomplete)")
        print(f"   {len(suggestions)+3}. Skip and mark as imported (never ask again)")
        print(f"   {len(suggestions)+4}. Skip for now (ask again next run)")
        print(f"   {len(suggestions)+5}. Manage mappings")
        try:
            choice = int(input(f"  Enter choice (1-{len(suggestions)+5}): "))
        except ValueError:
            return None
        if 1 <= choice <= len(suggestions):
            account = suggestions[choice-1][0]
            if input("  Save mapping? [Y]/n: ").strip().lower() in ["", "y", "yes"]:
                self.payee_mapper.add_mapping(tx["payee"], account.fullname, account.guid)
            return account
        if choice == len(suggestions)+1:
            path = input(f"  New account path (default {suggested_path}): ").strip() or suggested_path
            account = self._new_acct(path, tx)
            if account and input("  Save mapping? [Y]/n: ").strip().lower() in ["", "y", "yes"]:
                self.payee_mapper.add_mapping(tx["payee"], account.fullname, account.guid)
            return account
        if choice == len(suggestions)+2:
            completer = AccountPathCompleter([account.fullname for account in self.matcher.accounts_cache.values()])
            path = _input_with_completion("  Account path (Tab to autocomplete): ", completer).strip()
            if not path:
                return None
            guid = self.matcher.get_account_guid(path)
            account = self.matcher.get_account_by_guid(guid) if guid else self._new_acct(path, tx)
            if account and input("  Save mapping? [Y]/n: ").strip().lower() in ["", "y", "yes"]:
                self.payee_mapper.add_mapping(tx["payee"], account.fullname, account.guid)
            return account
        if choice == len(suggestions)+3:
            if not self.dry_run:
                self._mark_skipped(tx, "user skipped and marked as imported")
            return None
        if choice == len(suggestions)+4:
            print("  Skipped for now (will be shown again next run)")
            return None
        if choice == len(suggestions)+5:
            self._manage_mappings()
            return self._manual_sel(tx, suggestions)
        return None

    def _manage_mappings(self):
        while True:
            mappings = self.payee_mapper.list_mappings()
            if not mappings:
                print("  No mappings stored.")
            else:
                for i, (payee, data) in enumerate(mappings[:10], 1):
                    print(f"   {i}. '{payee}' -> {data['account_fullname']}")
            choice = input("  v - view all, d - delete, q - quit: ").strip().lower()
            if choice == "q":
                return
            if choice == "v":
                self._view_all_mappings()
            elif choice == "d":
                self._del_mapping_interactive()

    def _view_all_mappings(self):
        for payee, data in self.payee_mapper.list_mappings():
            print(f"  '{payee}' -> {data['account_fullname']}")

    def _del_mapping_interactive(self):
        mappings = self.payee_mapper.list_mappings()
        for i, (payee, data) in enumerate(mappings, 1):
            print(f"   {i}. '{payee}' -> {data['account_fullname']}")
        try:
            choice = int(input("  Number: "))
            if 1 <= choice <= len(mappings):
                self.payee_mapper.remove_mapping(mappings[choice-1][0])
        except ValueError:
            pass

    def _new_acct(self, path, tx):
        if self.dry_run:
            print("  [DRY RUN] Would create account, but skipping actual creation")
            return None
        parts = path.split(":")
        if len(parts) < 2:
            print("  Path needs at least two parts")
            return None
        parent = None
        current = ""
        created = False
        for part in parts:
            current = f"{current}:{part}" if current else part
            guid = self.matcher.get_account_guid(current)
            if guid:
                parent = self.matcher.get_account_by_guid(guid)
                continue
            account = piecash.Account(
                name=part,
                type=self._infer_type(part),
                parent=parent if parent else self.book.root_account,
                commodity=self._get_or_create_commodity(tx["currency"]),
            )
            self.book.add(account)
            self.matcher.accounts_cache[account.guid] = account
            parent = account
            created = True
            print(f"  Created {current} ({tx['currency']})")
        if created:
            self._create_database_backup()
            self.book.save()
            print(f"  Saved new account(s) to {self.gnucash_file}")
        return parent

    @staticmethod
    def _infer_type(name):
        text = name.lower()
        if text.startswith("income") or "salary" in text or "revenue" in text:
            return "INCOME"
        if text.startswith("expense"):
            return "EXPENSE"
        if text.startswith("asset") or "bank" in text or "cash" in text:
            return "ASSET"
        if text.startswith("liability") or "loan" in text or "credit" in text:
            return "LIABILITY"
        if text.startswith("equity") or "capital" in text:
            return "EQUITY"
        return "EXPENSE"

    def _prepare_tx(self, account, tx):
        self.tx_to_create.append({
            "source_account": self.source_account,
            "dest_account": account,
            "date": tx["date"],
            "payee": tx["payee"],
            "amount": Decimal(str(tx["amount"])),
            "commodity": self._get_or_create_commodity(tx["currency"]),
            "memo": tx.get("memo", ""),
            "hash": tx["import_hash"],
        })
        print(f"  Prepared: {self.source_account.fullname} <-> {account.fullname} ({tx['currency']})")

    def _backup_directory(self):
        return os.path.join(os.path.dirname(self.gnucash_file), "backups")

    def _create_database_backup(self):
        """Copy the SQLite book once per write run before the first write.
        Older timestamped backups are pruned according to keep_backups."""
        if self.dry_run or self._backup_created:
            return
        backup_dir = self._backup_directory()
        os.makedirs(backup_dir, exist_ok=True)
        stem, ext = os.path.splitext(os.path.basename(self.gnucash_file))
        extension = ext or ".gnucash"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(backup_dir, f"{stem}.backup-{timestamp}{extension}")
        shutil.copy2(self.gnucash_file, backup_path)
        self._backup_created = True
        print(f"Created database backup: {backup_path}")
        self._prune_backups(backup_dir, stem, extension)

    def _prune_backups(self, backup_dir, stem, extension):
        """Keep the newest keep_backups files. Zero means keep all."""
        if self.keep_backups <= 0:
            return
        prefix = f"{stem}.backup-"
        backups = sorted(
            (
                os.path.join(backup_dir, filename)
                for filename in os.listdir(backup_dir)
                if filename.startswith(prefix) and filename.endswith(extension)
            ),
            key=os.path.getmtime,
            reverse=True,
        )
        for old_backup in backups[self.keep_backups:]:
            os.remove(old_backup)
            print(f"Deleted old backup: {old_backup}")

    def execute_import(self):
        if not self.tx_to_create:
            print("  No transactions to import")
            return
        print("\n" + "=" * 70)
        print("EXECUTING IMPORT")
        print("=" * 70)
        print(f"Creating {len(self.tx_to_create)} transactions...")
        created = 0
        for info in self.tx_to_create:
            commodity = info["commodity"]
            amount = info["amount"]
            post_date = parse_tx_date(info["date"])
            transaction = piecash.Transaction(
                currency=commodity,
                description=info["payee"][:250],
                post_date=post_date.date(),
                enter_date=datetime.now(),
                splits=[
                    piecash.Split(account=info["source_account"], value=amount, memo=info["memo"][:200]),
                    piecash.Split(account=info["dest_account"], value=-amount, memo=info["memo"][:200]),
                ],
            )
            self.book.add(transaction)
            self.imported_tx[info["hash"]] = {
                "timestamp": datetime.now().isoformat(),
                "payee": info["payee"],
                "amount": str(amount),
                "currency": commodity.mnemonic,
                "source_account": info["source_account"].fullname,
                "dest_account": info["dest_account"].fullname,
                "csv_date": info["date"],
                "tx_guid": transaction.guid,
            }
            created += 1
            print(f"  {info['payee'][:50]} - {abs(amount):.2f} {commodity.mnemonic} on {post_date.date()}")
        if created:
            self._create_database_backup()
            self.book.save()
            self._save_imported()
            print(f"\n{created} transactions saved to {self.gnucash_file}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Import bank CSV into GnuCash")
    parser.add_argument("--gnucash-file", default=DEFAULT_GNUCASH_FILE)
    parser.add_argument("--csv-file", default=DEFAULT_CSV_FILE)
    parser.add_argument("--source-account", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--auto-accept", action="store_true")
    parser.add_argument("--no-ledger-check", action="store_true")
    parser.add_argument("--keep-backups", type=int, default=DEFAULT_KEEP_BACKUPS,
                        help="Number of timestamped database backups to retain; 0 keeps all (default: 10)")
    parser.add_argument("--list-mappings", action="store_true")
    parser.add_argument("--clear-mappings", action="store_true")
    parser.add_argument("--clear-history", action="store_true")
    parser.add_argument("--export-accounts", action="store_true")
    parser.add_argument("--include-pending", action="store_true")
    args = parser.parse_args()

    payee_mapper = PayeeAccountMapper()
    if args.list_mappings:
        for payee, mapping in payee_mapper.list_mappings():
            print(f"'{payee}' -> {mapping['account_fullname']}")
        return
    if args.clear_mappings:
        if input("Delete ALL mappings? [y/N]: ").strip().lower() == "y":
            payee_mapper.mappings = {}
            payee_mapper._save_mappings()
        return
    if args.clear_history:
        if os.path.exists(TRANSACTION_HISTORY_FILE):
            os.remove(TRANSACTION_HISTORY_FILE)
        return

    importer = None
    try:
        importer = TransactionImporter(args.gnucash_file, args.csv_file)
        importer.dry_run = args.dry_run
        importer.auto_accept = args.auto_accept
        importer.check_ledger = not args.no_ledger_check
        importer.keep_backups = max(0, args.keep_backups)
        importer.payee_mapper = payee_mapper
        importer.open_book(readonly=args.dry_run)

        if args.export_accounts:
            importer.export_accounts_json()
            return

        importer.history_analyzer = TransactionHistoryAnalyzer(importer.book)
        importer.history_analyzer.analyze_transactions()
        importer.matcher = AccountMatcher(importer.book, importer.payee_mapper, importer.history_analyzer)
        importer.source_account = importer.resolve_source_account(args.source_account)
        rows = importer.read_csv(skip_pending=not args.include_pending)
        if not rows:
            print("No transactions in CSV")
            return
        importer.process_transactions(rows)
        if importer.tx_to_create:
            print(f"\nPrepared {len(importer.tx_to_create)} transactions for import")
            if not args.dry_run:
                if args.auto_accept or input("\nExecute import now? [y/N]: ").strip().lower() == "y":
                    importer.execute_import()
                else:
                    print("Import cancelled")
        else:
            print("\n[DRY RUN] Run again without --dry-run to execute")
    except KeyboardInterrupt:
        print("\nCancelled by user")
    except Exception as exc:
        print(f"\nError: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        if importer is not None and importer.book is not None:
            try:
                importer.book.close()
                print("GnuCash book closed - lock released.")
            except Exception as close_error:
                print(f"Warning: could not cleanly close book: {close_error}")


if __name__ == "__main__":
    main()
