#!/usr/bin/env python3
"""
GnuCash CSV Importer with Transaction History Analysis and Currency Support
==========================================================================
Automatically learns from your existing transactions to suggest mappings.
Supports GBP (default), USD, and EUR currencies for accounts.

Works against a SQLite-backed GnuCash book (piecash cannot read/write
GnuCash XML files directly -- convert via GnuCash's File -> Save As ->
sqlite3 first if your book is still in XML format).

Every transaction is recorded as a proper double-entry split between the
SOURCE account (the bank/Revolut account the CSV export belongs to) and the
DESTINATION account (the expense/income category chosen or suggested), and
uses the transaction date FROM THE CSV (not the import run date).
"""

import csv
import json
import os
import re
import warnings
import piecash
import hashlib
from decimal import Decimal
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from typing import Dict, List, Optional, Tuple, Set
import difflib

from sqlalchemy import exc as sa_exc

warnings.filterwarnings("ignore", category=sa_exc.SAWarning)

DEFAULT_GNUCASH_FILE = "portfolio-sqlite.gnucash"
DEFAULT_CSV_FILE = "transactions.csv"
BASE_CURRENCY = "GBP"
MAX_ACCOUNT_SUGGESTIONS = 5
DUPLICATE_CHECK_FILE = ".imported_transactions.json"
MAPPINGS_FILE = ".payee_account_mappings.json"
TRANSACTION_HISTORY_FILE = ".transaction_history_analysis.json"
ACCOUNTS_EXPORT_FILE = "accounts.json"
SUPPORTED_CURRENCIES = {"GBP", "USD", "EUR"}


def parse_tx_date(date_str: str) -> datetime:
    """Parse a CSV date string into a datetime, trying several common
    formats (Revolut's 'YYYY-MM-DD HH:MM:SS', plain 'YYYY-MM-DD', and
    UK-style 'DD/MM/YYYY' as used by some bank exports). Only falls back
    to now() if the string is empty or genuinely unparseable, and always
    prints a warning so the mismatch is visible rather than silent."""
    date_str = (date_str or "").strip()
    if not date_str:
        print("  Warning: empty date field - using current date/time as fallback")
        return datetime.now()

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
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
    """readline completer that offers existing GnuCash account fullnames
    as tab-completion candidates when typing an account path."""

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
    """Prompt for input with tab-completion enabled, falling back to plain
    input() if readline is unavailable (e.g. some Windows terminals)."""
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
    """Analyzes past transactions to learn payee -> account patterns"""

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
                    for w in self._extract_words(payee):
                        self.word_patterns[w][account_full] += 1

        self.payee_to_accounts = defaultdict(list)
        for payee, accts in payee_usage.items():
            top_count = max(accts.values())
            for acct, cnt in accts.most_common(5):
                self.payee_to_accounts[payee].append(
                    {
                        "account": acct,
                        "count": cnt,
                        "confidence": min(cnt / top_count, 1.0),
                    }
                )

        self.analysis_cache = {
            "last_analyzed": datetime.now().isoformat(),
            "transactions_analyzed": len(txns),
            "payee_mappings": {
                p: [
                    {
                        "account": a["account"],
                        "count": a["count"],
                        "confidence": a["confidence"],
                    }
                    for a in accts
                ]
                for p, accts in self.payee_to_accounts.items()
            },
            "word_patterns": {
                w: dict(cnts.most_common(10)) for w, cnts in self.word_patterns.items()
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
        return [w for w in words if len(w) > 2 and w not in stop]

    def suggest_mapping(
        self, csv_payee: str, csv_description: str = ""
    ) -> List[Tuple[str, float, str]]:
        """Return [(account_fullname, confidence, reason), ...]"""
        suggestions = []
        payee_l = csv_payee.lower().strip()
        desc_l = csv_description.lower().strip()

        if csv_payee in self.payee_to_accounts:
            for m in self.payee_to_accounts[csv_payee]:
                suggestions.append(
                    (
                        m["account"],
                        m["confidence"],
                        f"Exact match: '{csv_payee}' used {m['count']} times",
                    )
                )

        for payee, mlist in self.payee_to_accounts.items():
            sim = self._similarity(payee_l, payee.lower())
            if sim > 0.7:
                for m in mlist:
                    suggestions.append(
                        (
                            m["account"],
                            m["confidence"] * sim,
                            f"Fuzzy match: '{payee}' (similarity {sim:.1%})",
                        )
                    )

        csv_words = set(self._extract_words(csv_payee + " " + csv_description))
        word_scores = defaultdict(float)
        for w in csv_words:
            accounts_for_word = self.word_patterns.get(w, {})
            if not accounts_for_word:
                continue
            distinct_accounts = len(accounts_for_word)
            rarity_weight = 1.0 / distinct_accounts
            for acct, cnt in accounts_for_word.items():
                word_scores[acct] += cnt * rarity_weight
        matched_words = sorted(
            (w for w in csv_words if w in self.word_patterns),
            key=lambda w: len(self.word_patterns.get(w, {})),
        )
        for acct, sc in Counter(word_scores).most_common(5):
            if sc > 0.15:
                word_hint = matched_words[0] if matched_words else ""
                suggestions.append(
                    (
                        acct,
                        min(sc, 0.9),
                        f"Word pattern: '{word_hint}' appears in similar transactions",
                    )
                )

        if csv_description:
            for payee, mlist in self.payee_to_accounts.items():
                if desc_l in payee.lower() or payee.lower() in desc_l:
                    for m in mlist:
                        suggestions.append(
                            (
                                m["account"],
                                m["confidence"] * 0.9,
                                f"Description match: '{csv_description}' relates to '{payee}'",
                            )
                        )

        best = {}
        for acct, conf, reason in suggestions:
            if acct not in best or conf > best[acct][0]:
                best[acct] = (conf, reason)
        return [(acct, conf, reason) for acct, (conf, reason) in best.items()]

    @staticmethod
    def _similarity(s1: str, s2: str) -> float:
        return difflib.SequenceMatcher(None, s1, s2).ratio()

    def get_common_payees(self, limit: int = 20) -> List[Tuple[str, int]]:
        return [
            (p, sum(m["count"] for m in ms)) for p, ms in self.payee_to_accounts.items()
        ][:limit]


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
        try:
            with open(self.mappings_file, "w") as f:
                json.dump(self.mappings, f, indent=2, sort_keys=True)
        except Exception as e:
            print(f"Could not save mappings: {e}")

    def get_mapping(self, payee: str) -> Optional[Dict]:
        if payee in self.mappings:
            return self.mappings[payee]
        pl = payee.lower().strip()
        for sp, m in self.mappings.items():
            spl = sp.lower().strip()
            if spl in pl or pl in spl:
                if len(spl) >= 3 and len(pl) >= 3:
                    return m
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
            self.mappings[payee]["use_count"] = (
                self.mappings[payee].get("use_count", 0) + 1
            )
            self._save_mappings()

    def remove_mapping(self, payee: str) -> bool:
        if payee in self.mappings:
            del self.mappings[payee]
            self._save_mappings()
            return True
        return False

    def list_mappings(self) -> List[Tuple[str, Dict]]:
        return sorted(
            self.mappings.items(), key=lambda x: x[1].get("use_count", 0), reverse=True
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
        memo_parts = [p for p in [row.get("Type", ""), state] if p]
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
        date_str = row.get("Posting Date") or row.get("Value Date", "")
        raw = row.get("Debit") or row.get("Credit")
        if raw:
            amt = float(str(raw).replace("£", "").replace(",", ".").strip())
            sign = -1 if row.get("Debit") else 1
        else:
            amt = 0.0
            sign = 1
        return {
            "date": date_str,
            "payee": row.get("Description") or row.get("Payee", ""),
            "amount": amt * sign,
            "currency": row.get("Currency", "").upper() or BASE_CURRENCY,
            "memo": row.get("Reference") or row.get("Notes", ""),
            "original_row": row,
        }

    @staticmethod
    def detect_format(csv_file: str) -> str:
        try:
            with open(csv_file, "r", encoding="utf-8-sig", newline="") as f:
                header = next(csv.reader(f))
            hl = [h.strip().lower() for h in header]
            ho = [h.strip() for h in header]

            revolut_signals = ["started date", "completed date", "product", "state"]
            if any("counterparty" in h for h in hl) or sum(
                sig in hl for sig in revolut_signals
            ) >= 2:
                return "revolut"

            if any("posting date" == h.lower() for h in ho) or any(
                "posting date" in h for h in hl
            ):
                return "bof_scot"

            required = ["date", "payee", "amount"]
            if all(any(rc in h for h in hl) for rc in required):
                return "generic"
        except Exception as e:
            print(f"Warning: Could not detect format: {e}")
        return "generic"

    @staticmethod
    def map_generic(row: dict) -> dict:
        lower_row = {k.lower().strip(): v for k, v in row.items()}
        try:
            amt = float(
                str(lower_row.get("amount", "0")).replace("£", "").replace(",", ".").strip()
            )
        except (ValueError, AttributeError):
            amt = 0.0
        return {
            "date": lower_row.get("date", ""),
            "payee": lower_row.get("payee", ""),
            "amount": amt,
            "currency": lower_row.get("currency", "").upper() or BASE_CURRENCY,
            "memo": lower_row.get("memo", ""),
            "original_row": row,
        }


class AccountMatcher:
    def __init__(
        self,
        book: piecash.Book,
        payee_mapper: PayeeAccountMapper,
        history_analyzer: TransactionHistoryAnalyzer,
    ):
        self.book = book
        self.payee_mapper = payee_mapper
        self.history_analyzer = history_analyzer
        self.accounts_cache = {}
        self._cache_accounts()

    def _cache_accounts(self):
        for acct in self.book.accounts:
            self.accounts_cache[acct.guid] = acct

    @staticmethod
    def _acct_currency_matches(acct: object, code: str) -> bool:
        cur = getattr(acct, "commodity", None)
        if not cur:
            return False
        return (
            cur.namespace == "CURRENCY"
            and cur.mnemonic.upper() == code.upper()
        )

    @staticmethod
    def _extract_tokens(text: str) -> Set[str]:
        text = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
        words = text.split()
        stop = {
            "the", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by",
        }
        return set(w for w in words if len(w) > 2 and w not in stop)

    def _keyword_account_matches(
        self, payee: str, transaction_currency: str
    ) -> List[Tuple[object, float, str]]:
        payee_tokens = self._extract_tokens(payee)
        if not payee_tokens:
            return []

        matches = []
        for acct in self.accounts_cache.values():
            if not self._acct_currency_matches(acct, transaction_currency):
                continue
            acct_tokens = self._extract_tokens(acct.fullname.replace(":", " "))
            overlap = payee_tokens & acct_tokens
            if not overlap:
                continue
            score = len(overlap) / len(payee_tokens)
            if any(len(w) >= 5 for w in overlap):
                score = min(score + 0.15, 0.9)
            reason = f"Keyword match: shared word(s) {', '.join(sorted(overlap))!r}"
            matches.append((acct, round(score, 2), reason))

        matches.sort(key=lambda x: x[1], reverse=True)
        return matches

    def find_matching_accounts(
        self,
        payee: str,
        description: str = "",
        transaction_currency: str = BASE_CURRENCY,
        max_suggestions: int = MAX_ACCOUNT_SUGGESTIONS,
        exclude_guid: Optional[str] = None,
    ) -> List[Tuple[object, float, str]]:
        all_sug = []

        mm = self.payee_mapper.get_mapping(payee)
        if mm:
            acct = self.get_account_by_guid(mm["account_guid"])
            if acct and self._acct_currency_matches(acct, transaction_currency):
                all_sug.append(
                    (acct, 1.0, f"Manual mapping (used {mm.get('use_count', 1)} times)")
                )

        for acct_full, conf, reason in self.history_analyzer.suggest_mapping(
            payee, description
        ):
            acct = self._find_account_by_fullname(acct_full)
            if acct and self._acct_currency_matches(acct, transaction_currency):
                all_sug.append((acct, conf, f"History: {reason}"))

        payee_l = payee.lower().strip()
        for acct in self.accounts_cache.values():
            if not self._acct_currency_matches(acct, transaction_currency):
                continue
            an_l = acct.name.lower().strip()
            fn_l = acct.fullname.lower().strip()
            if payee_l == an_l or payee_l == fn_l:
                all_sug.append((acct, 0.95, "Exact account name match"))
            elif payee_l in an_l or payee_l in fn_l:
                all_sug.append((acct, 0.8, "Account name contains payee"))
            elif an_l.find(payee_l) != -1 or fn_l.find(payee_l) != -1:
                all_sug.append((acct, 0.7, "Payee contained in account name"))

        all_sug.extend(
            self._keyword_account_matches(payee, transaction_currency)
        )

        if exclude_guid:
            all_sug = [s for s in all_sug if s[0].guid != exclude_guid]

        best = {}
        for ac, conf, reason in all_sug:
            if ac.guid not in best or conf > best[ac.guid][0]:
                best[ac.guid] = (conf, reason, ac)
        sorted_sug = sorted(
            [(ac, conf, reason) for ac, (conf, reason, ac) in best.items()],
            key=lambda x: x[1],
            reverse=True,
        )
        return sorted_sug[:max_suggestions]

    def _find_account_by_fullname(self, fullname: str) -> Optional[object]:
        for acct in self.accounts_cache.values():
            if acct.fullname == fullname:
                return acct
        return None

    @staticmethod
    def suggest_category(payee: str) -> str:
        pl = payee.lower()
        if any(
            w in pl
            for w in ["tesco", "sainsbury", "asda", "supermarket", "food", "waitrose", "aldi", "lidl"]
        ):
            return "Expenses:Food"
        if any(w in pl for w in ["amazon", "ebay", "shop", "retail", "asos", "next", "argos"]):
            return "Expenses:Shopping"
        if any(
            w in pl
            for w in ["shell", "bp", "esso", "petrol", "fuel", "transport", "uber", "train", "bus", "car park", "parking"]
        ):
            return "Expenses:Transportation"
        if any(w in pl for w in ["netflix", "spotify", "subscription", "monthly", "disney", "prime"]):
            return "Expenses:Subscriptions"
        if any(w in pl for w in ["salary", "wages", "income", "payment", "payroll", "hmrc", "tax"]):
            return "Income:Salary"
        if any(
            w in pl
            for w in ["electric", "gas", "water", "broadband", "internet", "phone", "utilities", "council"]
        ):
            return "Expenses:Utilities"
        return "Expenses:Miscellaneous"

    @staticmethod
    def _suggest_new_account_path(
        payee: str, sugg: List[Tuple[object, float, str]], fallback_category: str
    ) -> str:
        if sugg:
            top_account = sugg[0][0]
            parts = top_account.fullname.split(":")
            if len(parts) >= 2:
                category_prefix = ":".join(parts[:2])
                return f"{category_prefix}:{payee}"
        return f"{fallback_category}:{payee}"

    def get_account_by_guid(self, guid: str) -> Optional[object]:
        return self.accounts_cache.get(guid)

    def get_account_guid(self, name: str) -> Optional[str]:
        for acct in self.accounts_cache.values():
            if acct.name == name or acct.fullname == name:
                return acct.guid
        return None


class TransactionImporter:
    def __init__(self, gnucash_file: str, csv_file: str):
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
        self.accts_to_create = []

    def _load_imported(self) -> dict:
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

    def _tx_hash(self, tx: dict) -> str:
        d = f"{tx['date']}|{tx['payee']}|{tx['amount']:.2f}"
        return hashlib.md5(d.encode("utf-8")).hexdigest()

    def _get_or_create_commodity(self, code: str) -> piecash.Commodity:
        code = code.upper()
        if code not in SUPPORTED_CURRENCIES:
            print(f"{code} not supported - falling back to {BASE_CURRENCY}")
            code = BASE_CURRENCY
        for c in self.book.commodities:
            if c.namespace == "CURRENCY" and c.mnemonic == code:
                return c
        new_c = piecash.Commodity(
            name=code,
            namespace="CURRENCY",
            mnemonic=code,
            fullname=f"{code} Currency",
            quote_source="Manual",
        )
        self.book.commodities.append(new_c)
        print(f"Created new currency commodity: {code}")
        return new_c

    def open_book(self, readonly: bool = True):
        self.gnucash_file = os.path.abspath(self.gnucash_file)
        if not os.path.exists(self.gnucash_file):
            raise FileNotFoundError(f"GnuCash file not found: {self.gnucash_file}")

        self.book = piecash.open_book(
            self.gnucash_file,
            readonly=readonly,
            open_if_lock=True,
        )

    def resolve_source_account(self, path_hint: Optional[str] = None) -> object:
        if path_hint:
            guid = self.matcher.get_account_guid(path_hint)
            if guid:
                acct = self.matcher.get_account_by_guid(guid)
                print(f"Using source account: {acct.fullname}")
                return acct
            print(f"Warning: account '{path_hint}' not found - please select manually.")

        asset_accounts = sorted(
            (
                a
                for a in self.matcher.accounts_cache.values()
                if a.type == "ASSET" and a.placeholder == 0
            ),
            key=lambda a: a.fullname,
        )
        print("\nWhich account does this CSV export belong to?")
        for i, a in enumerate(asset_accounts, 1):
            print(f"  {i}. {a.fullname} ({a.commodity.mnemonic if a.commodity else '?'})")
        completer = AccountPathCompleter([a.fullname for a in asset_accounts])
        while True:
            choice = _input_with_completion(
                "Enter number or account path (Tab to autocomplete): ", completer
            ).strip()
            if choice.isdigit() and 1 <= int(choice) <= len(asset_accounts):
                return asset_accounts[int(choice) - 1]
            guid = self.matcher.get_account_guid(choice)
            if guid:
                return self.matcher.get_account_by_guid(guid)
            print("  Not recognized - try again.")

    def export_accounts_json(self, json_path: str = ACCOUNTS_EXPORT_FILE):
        accounts = {}
        for acct in self.book.accounts:
            accounts[acct.guid] = {
                "guid": acct.guid,
                "name": acct.name,
                "fullname": acct.fullname,
                "type": acct.type,
                "description": acct.description or "",
                "currency": acct.commodity.mnemonic if acct.commodity else None,
                "parent_guid": acct.parent.guid if acct.parent else None,
            }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, sort_keys=True)
        print(f"Exported {len(accounts)} accounts to {json_path}")
        return accounts

    def read_csv(self, skip_pending: bool = True) -> List[dict]:
        if not os.path.exists(self.csv_file):
            raise FileNotFoundError(f"CSV not found: {self.csv_file}")
        fmt = self.mapper.detect_format(self.csv_file)
        print(f"Detected bank format: {fmt}")

        rows = []
        skipped_pending = 0
        with open(self.csv_file, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                if not any(str(v).strip() for v in row.values()):
                    continue
                try:
                    if fmt == "revolut":
                        m = self.mapper.map_revolut(row)
                    elif fmt == "bof_scot":
                        m = self.mapper.map_bof_scot(row)
                    else:
                        m = self.mapper.map_generic(row)

                    if skip_pending and str(m.get("state", "")).upper() == "PENDING":
                        skipped_pending += 1
                        continue

                    m["row_number"] = i
                    m["import_hash"] = self._tx_hash(m)
                    rows.append(m)
                except Exception as e:
                    print(f"Row {i}: mapping error - {e}")
        if skipped_pending:
            print(f"Skipped {skipped_pending} pending transaction(s) (not yet settled)")
        print(f"Read {len(rows)} transactions")
        return rows

    def process_transactions(self, transactions: List[dict]):
        print("\n" + "=" * 70)
        print("TRANSACTION PROCESSING")
        print("=" * 70)
        print(f"Source account: {self.source_account.fullname}")

        for idx, tx in enumerate(transactions, 1):
            print(f"\n[{idx}/{len(transactions)}] Row {tx['row_number']}")
            print(f"  Date: {tx['date']}")
            print(f"  Payee: {tx['payee']}")
            print(
                f"  Amount: {abs(tx['amount']):.2f} {tx['currency']}"
                f" {'(Expense)' if tx['amount'] <= 0 else '(Income)'}"
            )
            if tx.get("memo"):
                print(f"  Memo: {tx['memo']}")

            if tx["import_hash"] in self.imported_tx:
                print("  Already imported - skipping")
                continue

            mm = self.payee_mapper.get_mapping(tx["payee"])
            if mm:
                acct = self.matcher.get_account_by_guid(mm["account_guid"])
                if acct and self.matcher._acct_currency_matches(acct, tx["currency"]):
                    print(
                        f"\n  MAPPED: {mm['account_fullname']} "
                        f"(used {mm.get('use_count', 1)} times)"
                    )
                    if self.dry_run:
                        print("  [DRY RUN] Would use existing mapping")
                        self._prepare_tx(acct, tx)
                        continue
                    elif self.auto_accept:
                        print("  [AUTO-ACCEPT] Using existing mapping")
                        self.payee_mapper.update_last_used(tx["payee"])
                        self._prepare_tx(acct, tx)
                        continue
                    else:
                        ans = input("  Use this mapping? [Y]/n/edit: ").strip().lower()
                        if ans in ["", "y", "yes"]:
                            self.payee_mapper.update_last_used(tx["payee"])
                            self._prepare_tx(acct, tx)
                            continue
                        elif ans == "edit":
                            pass
                elif acct is None:
                    print(
                        f"  Mapping exists but the account no longer exists in "
                        f"the book (guid not found): {mm['account_fullname']}. "
                        f"Removing stale mapping."
                    )
                    self.payee_mapper.remove_mapping(tx["payee"])
                else:
                    print(
                        f"  Mapping exists but currency does not match this "
                        f"transaction ({tx['currency']}): {mm['account_fullname']}"
                    )

            sugg = self.matcher.find_matching_accounts(
                tx["payee"],
                tx.get("memo", ""),
                transaction_currency=tx["currency"],
                exclude_guid=self.source_account.guid,
            )
            if sugg:
                print("\n  Suggested accounts (filtered by currency):")
                for i, (ac, conf, reason) in enumerate(sugg, 1):
                    bar = "#" * int(conf * 10) + "-" * (10 - int(conf * 10))
                    print(f"   {i}. {ac.fullname} [{bar}] {conf:.0%}")
                    print(f"      {reason}")
            else:
                cat = self.matcher.suggest_category(tx["payee"])
                print("\n  No historical matches.")
                print(f"  Suggested category: {cat}")

            if self.dry_run:
                print("  [DRY RUN] Skipping manual selection")
                continue

            if self.auto_accept:
                if sugg:
                    top_acct = sugg[0][0]
                    print(f"  [AUTO-ACCEPT] Using top suggestion: {top_acct.fullname}")
                    self._prepare_tx(top_acct, tx)
                else:
                    print("  [AUTO-ACCEPT] No suggestions available - skipping transaction")
                continue

            sel = self._manual_sel(tx, sugg)
            if sel:
                self._prepare_tx(sel, tx)
            else:
                print("  Transaction skipped")

    def _manual_sel(
        self, tx: dict, sugg: List[Tuple[object, float, str]]
    ) -> Optional[object]:
        print("\n  Account Selection:")
        for i, (ac, conf, reason) in enumerate(sugg, 1):
            print(f"   {i}. {ac.fullname} ({conf:.0%} confidence)")
        fallback_cat = self.matcher.suggest_category(tx["payee"])
        suggested_path = self.matcher._suggest_new_account_path(
            tx["payee"], sugg, fallback_cat
        )
        print(f"   {len(sugg) + 1}. Create new account (suggested: {suggested_path})")
        print(f"   {len(sugg) + 2}. Enter account path manually (tab to autocomplete)")
        print(f"   {len(sugg) + 3}. Skip this transaction")
        print(f"   {len(sugg) + 4}. Manage mappings")

        try:
            ch = int(input(f"  Enter choice (1-{len(sugg) + 4}): "))
        except ValueError:
            print("  Invalid input - skipping")
            return None

        if 1 <= ch <= len(sugg):
            ac = sugg[ch - 1][0]
            if not self.dry_run and input(
                "  Save mapping? [Y]/n: "
            ).strip().lower() in ["", "y", "yes"]:
                self.payee_mapper.add_mapping(tx["payee"], ac.fullname, ac.guid)
                print(f"  Saved mapping: '{tx['payee']}' -> '{ac.fullname}'")
            return ac

        if ch == len(sugg) + 1:
            path = input(f"  New account path (default {suggested_path}): ").strip() or suggested_path
            ac = self._new_acct(path, tx)
            if (
                ac
                and not self.dry_run
                and input("  Save mapping? [Y]/n: ").strip().lower() in ["", "y", "yes"]
            ):
                self.payee_mapper.add_mapping(tx["payee"], ac.fullname, ac.guid)
                print(f"  Saved mapping: '{tx['payee']}' -> '{ac.fullname}'")
            return ac

        if ch == len(sugg) + 2:
            completer = AccountPathCompleter(
                [a.fullname for a in self.matcher.accounts_cache.values()]
            )
            path = _input_with_completion(
                "  Account path (Tab to autocomplete, Tab-Tab to list options): ",
                completer,
            ).strip()
            if not path:
                print("  No path entered - skipping")
                return None
            existing_guid = self.matcher.get_account_guid(path)
            if existing_guid:
                ac = self.matcher.get_account_by_guid(existing_guid)
                print(f"  Using existing account: {ac.fullname}")
            else:
                ac = self._new_acct(path, tx)
            if (
                ac
                and not self.dry_run
                and input("  Save mapping? [Y]/n: ").strip().lower() in ["", "y", "yes"]
            ):
                self.payee_mapper.add_mapping(tx["payee"], ac.fullname, ac.guid)
                print(f"  Saved mapping: '{tx['payee']}' -> '{ac.fullname}'")
            return ac

        if ch == len(sugg) + 3:
            print("  Skipped")
            return None
        if ch == len(sugg) + 4:
            self._manage_mappings()
            return self._manual_sel(tx, sugg)
        return None

    def _manage_mappings(self):
        while True:
            mlist = self.payee_mapper.list_mappings()
            if not mlist:
                print("  No mappings stored.")
            else:
                print(f"  {len(mlist)} mappings stored:")
                for i, (p, d) in enumerate(mlist[:10], 1):
                    print(
                        f"   {i}. '{p}' -> {d['account_fullname']} (used {d.get('use_count', 1)} times)"
                    )
                if len(mlist) > 10:
                    print(f"   ...and {len(mlist) - 10} more")
            print("  v - view all   d - delete   q - quit")
            c = input("  Choice: ").strip().lower()
            if c == "v":
                self._view_all_mappings()
            elif c == "d":
                self._del_mapping_interactive()
            elif c == "q":
                break
            else:
                print("  Invalid choice")

    def _view_all_mappings(self):
        for p, d in self.payee_mapper.list_mappings():
            print(f"  '{p}' -> {d['account_fullname']} (used {d.get('use_count', 1)} times)")

    def _del_mapping_interactive(self):
        mlist = self.payee_mapper.list_mappings()
        if not mlist:
            print("  None to delete")
            return
        print("  Select mapping number to delete (0 cancels):")
        for i, (p, d) in enumerate(mlist, 1):
            print(f"   {i}. '{p}' -> {d['account_fullname']}")
        try:
            n = int(input("  Number: "))
            if 1 <= n <= len(mlist):
                p = mlist[n - 1][0]
                if input(f"  Delete '{p}'? [y/N]: ").strip().lower() == "y":
                    self.payee_mapper.remove_mapping(p)
                    print("  Deleted")
        except ValueError:
            print("  Invalid input")

    def _new_acct(self, path: str, tx: dict) -> Optional[object]:
        if self.dry_run:
            print(
                "  [DRY RUN] Would create account, but skipping actual creation "
                "(dry-run never persists new accounts)"
            )
            return None

        parts = path.split(":")
        if len(parts) < 2:
            print("  Path needs at least two parts, e.g. Expenses:Food")
            return None
        parent = None
        cur = ""
        created_any = False
        for part in parts:
            cur = f"{cur}:{part}" if cur else part
            exist = self.matcher.get_account_guid(cur)
            if exist:
                parent = self.matcher.get_account_by_guid(exist)
                continue
            acct_type = self._infer_type(part)
            try:
                commod = self._get_or_create_commodity(tx["currency"])
                new = piecash.Account(
                    name=part,
                    type=acct_type,
                    parent=parent if parent else self.book.root_account,
                    commodity=commod,
                )
                self.book.add(new)
                print(f"  Created {cur} ({tx['currency']})")
                self.accts_to_create.append(new)
                self.matcher.accounts_cache[new.guid] = new
                parent = new
                created_any = True
            except Exception as e:
                print(f"  Could not create {cur}: {e}")
                return None

        if created_any:
            try:
                self.book.save()
                print(f"  Saved new account(s) to {self.gnucash_file}")
            except Exception as e:
                print(f"  Warning: could not save new account(s) immediately: {e}")

        return parent

    @staticmethod
    def _infer_type(name: str) -> str:
        nl = name.lower()
        if nl.startswith("income") or "salary" in nl or "revenue" in nl:
            return "INCOME"
        if nl.startswith("expense") or any(
            x in nl for x in ["food", "shopping", "transport", "utilities", "rent"]
        ):
            return "EXPENSE"
        if nl.startswith("asset") or "bank" in nl or "cash" in nl:
            return "ASSET"
        if nl.startswith("liability") or "loan" in nl or "credit" in nl:
            return "LIABILITY"
        if nl.startswith("equity") or "capital" in nl:
            return "EQUITY"
        return "EXPENSE"

    def _prepare_tx(self, acct: object, tx: dict):
        """Prepare a BALANCED double-entry transaction: one split against
        the source (bank/Revolut) account, one offsetting split against
        the chosen destination (expense/income) account. Keeps the raw
        CSV date string so execute_import() can parse and apply it as the
        transaction's real post_date instead of defaulting to today."""
        commod = self._get_or_create_commodity(tx["currency"])
        amt = Decimal(str(tx["amount"]))
        self.tx_to_create.append(
            {
                "source_account": self.source_account,
                "dest_account": acct,
                "date": tx["date"],
                "payee": tx["payee"],
                "amount": amt,
                "commodity": commod,
                "memo": tx.get("memo", ""),
                "hash": tx["import_hash"],
            }
        )
        print(f"  Prepared: {self.source_account.fullname} <-> {acct.fullname} ({tx['currency']}) dated {tx['date']}")

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
            try:
                commod = info["commodity"]
                source_value = info["amount"]
                dest_value = -info["amount"]
                post_date = parse_tx_date(info["date"])
                tx = piecash.Transaction(
                    currency=commod,
                    description=info["payee"][:250],
                    post_date=post_date.date(),
                    enter_date=datetime.now(),
                    splits=[
                        piecash.Split(
                            account=info["source_account"],
                            value=source_value,
                            memo=info["memo"][:200],
                        ),
                        piecash.Split(
                            account=info["dest_account"],
                            value=dest_value,
                            memo=info["memo"][:200],
                        ),
                    ],
                )
                self.book.add(tx)
                self.imported_tx[info["hash"]] = {
                    "timestamp": datetime.now().isoformat(),
                    "payee": info["payee"],
                    "amount": str(info["amount"]),
                    "currency": commod.mnemonic,
                    "source_account": info["source_account"].fullname,
                    "dest_account": info["dest_account"].fullname,
                    "csv_date": info["date"],
                    "tx_guid": tx.guid,
                }
                created += 1
                print(
                    f"  {info['payee'][:50]} - {abs(info['amount']):.2f} {commod.mnemonic} "
                    f"on {post_date.date()} "
                    f"({info['source_account'].fullname} <-> {info['dest_account'].fullname})"
                )
            except Exception as e:
                print(f"  Failed {info['payee']}: {e}")

        if created:
            self.book.save()
            self._save_imported()
            print(f"\n{created} transactions saved to {self.gnucash_file}")
        else:
            print("\nNo transactions created")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Import bank CSV into GnuCash with history-based suggestions "
        "and currency support (GBP/USD/EUR)"
    )
    parser.add_argument(
        "--gnucash-file", default=DEFAULT_GNUCASH_FILE, help="Path to .gnucash (sqlite) file"
    )
    parser.add_argument(
        "--csv-file", default=DEFAULT_CSV_FILE, help="Path to bank CSV export"
    )
    parser.add_argument(
        "--source-account",
        default=None,
        help="Fullname of the GnuCash Asset account this CSV export belongs "
        "to (e.g. 'Assets:Current Assets:Revolut GBP'). Prompted "
        "interactively if omitted.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without saving"
    )
    parser.add_argument(
        "--auto-accept",
        action="store_true",
        help="Auto-accept the top suggestion for every row without prompting "
        "(still writes to the book unless combined with --dry-run)",
    )
    parser.add_argument(
        "--list-mappings",
        action="store_true",
        help="List stored payee->account mappings and exit",
    )
    parser.add_argument(
        "--clear-mappings", action="store_true", help="Delete all stored mappings"
    )
    parser.add_argument(
        "--clear-history",
        action="store_true",
        help="Delete the transaction-history cache",
    )
    parser.add_argument(
        "--export-accounts",
        action="store_true",
        help="Export the account tree to accounts.json and exit",
    )
    parser.add_argument(
        "--include-pending",
        action="store_true",
        help="Include PENDING (not-yet-settled) Revolut transactions",
    )
    args = parser.parse_args()

    payee_mapper = PayeeAccountMapper()

    if args.list_mappings:
        mlist = payee_mapper.list_mappings()
        if mlist:
            print(f"\nStored mappings ({len(mlist)}):")
            for p, d in mlist:
                print(f"  '{p}' -> {d['account_fullname']} ({d.get('use_count', 1)} times)")
        else:
            print("\nNo mappings stored.")
        return

    if args.clear_mappings:
        if input("Delete ALL mappings? [y/N]: ").strip().lower() == "y":
            payee_mapper.mappings = {}
            payee_mapper._save_mappings()
            print("All mappings deleted")
        else:
            print("Cancelled")
        return

    if args.clear_history:
        if os.path.exists(TRANSACTION_HISTORY_FILE):
            os.remove(TRANSACTION_HISTORY_FILE)
            print("History cache cleared")
        else:
            print("No cache found")
        return

    imp = None
    try:
        imp = TransactionImporter(args.gnucash_file, args.csv_file)
        imp.dry_run = args.dry_run
        imp.auto_accept = args.auto_accept
        imp.payee_mapper = payee_mapper

        imp.open_book(readonly=args.dry_run)

        if args.export_accounts:
            imp.export_accounts_json()
            return

        imp.history_analyzer = TransactionHistoryAnalyzer(imp.book)
        imp.history_analyzer.analyze_transactions()
        imp.matcher = AccountMatcher(imp.book, imp.payee_mapper, imp.history_analyzer)

        imp.source_account = imp.resolve_source_account(args.source_account)

        rows = imp.read_csv(skip_pending=not args.include_pending)

        if not rows:
            print("No transactions in CSV")
            return

        imp.process_transactions(rows)

        if imp.tx_to_create:
            print(f"\nPrepared {len(imp.tx_to_create)} transactions for import")
            if not args.dry_run:
                if args.auto_accept or input(
                    "\nExecute import now? [y/N]: "
                ).strip().lower() == "y":
                    imp.execute_import()
                else:
                    print("Import cancelled")
        else:
            print("\n[DRY RUN] Run again without --dry-run to execute")

    except KeyboardInterrupt:
        print("\nCancelled by user")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback

        traceback.print_exc()
    finally:
        if imp is not None and imp.book is not None:
            try:
                imp.book.close()
                print("GnuCash book closed - lock released.")
            except Exception as close_err:
                print(f"Warning: could not cleanly close book: {close_err}")


if __name__ == "__main__":
    main()
