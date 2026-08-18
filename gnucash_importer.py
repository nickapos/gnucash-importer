#!/usr/bin/env python3
"""
GnuCash CSV Importer with Transaction History Analysis and Currency Support
==========================================================================
Automatically learns from your existing transactions to suggest mappings.
Supports GBP (default), USD, and EUR currencies for accounts.

Works against a SQLite-backed GnuCash book (piecash cannot read/write
GnuCash XML files directly -- convert via GnuCash's File -> Save As ->
sqlite3 first if your book is still in XML format).
"""

import csv
import json
import os
import warnings
import piecash
import hashlib
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from typing import Dict, List, Optional, Tuple, Set
import difflib

from sqlalchemy import exc as sa_exc

# Silence piecash/SQLAlchemy 1.4 "overlapping relationship" warnings --
# these are cosmetic issues inside piecash's own ORM mapping and do not
# affect correctness.
warnings.filterwarnings("ignore", category=sa_exc.SAWarning)

# Configuration
DEFAULT_GNUCASH_FILE = "portfolio-sqlite.gnucash"
DEFAULT_CSV_FILE = "transactions.csv"
BASE_CURRENCY = "GBP"  # Default currency if none specified
MAX_ACCOUNT_SUGGESTIONS = 5
DUPLICATE_CHECK_FILE = ".imported_transactions.json"
MAPPINGS_FILE = ".payee_account_mappings.json"
TRANSACTION_HISTORY_FILE = ".transaction_history_analysis.json"
ACCOUNTS_EXPORT_FILE = "accounts.json"
SUPPORTED_CURRENCIES = {"GBP", "USD", "EUR"}  # Currencies we handle


# --------------------------------------------------------------
# 1. Transaction-history analyzer
# --------------------------------------------------------------
class TransactionHistoryAnalyzer:
    """Analyzes past transactions to learn payee -> account patterns"""

    def __init__(self, book: piecash.Book):
        self.book = book
        self.payee_to_accounts = defaultdict(list)  # payee -> [{account, count, confidence}]
        self.word_patterns = defaultdict(Counter)    # word -> {account: count}
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

        payee_usage = defaultdict(Counter)  # payee -> {account_fullname: count}

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

        # Keep top 5 accounts per payee
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
        import re

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

        # 1. exact payee match
        if csv_payee in self.payee_to_accounts:
            for m in self.payee_to_accounts[csv_payee]:
                suggestions.append(
                    (
                        m["account"],
                        m["confidence"],
                        f"Exact match: '{csv_payee}' used {m['count']} times",
                    )
                )

        # 2. fuzzy payee match (similarity > 70%)
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

        # 3. word-pattern matching
        csv_words = set(self._extract_words(csv_payee + " " + csv_description))
        word_scores = defaultdict(float)
        for w in csv_words:
            for acct, cnt in self.word_patterns.get(w, {}).items():
                word_scores[acct] += cnt * 0.1
        matched_words = [w for w in csv_words if w in self.word_patterns]
        for acct, sc in Counter(word_scores).most_common(5):
            if sc > 0.3:
                word_hint = matched_words[0] if matched_words else ""
                suggestions.append(
                    (
                        acct,
                        min(sc, 0.9),
                        f"Word pattern: '{word_hint}' appears in similar transactions",
                    )
                )

        # 4. description matching
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

        # de-duplicate, keep highest confidence per account
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


# --------------------------------------------------------------
# 2. Payee-to-account mapper (persistent JSON file)
# --------------------------------------------------------------
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
        # fuzzy fallback
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


# --------------------------------------------------------------
# 3. Bank-format mapper (Revolut / Bank-of-Scotland / generic)
# --------------------------------------------------------------
class BankFormatMapper:
    @staticmethod
    def map_revolut(row: dict) -> dict:
        return {
            "date": row.get("date") or row.get("transaction_date", ""),
            "payee": row.get("counterparty")
            or row.get("description")
            or row.get("merchant_name", ""),
            "amount": float(str(row.get("amount", "0")).replace(",", ".")),
            "currency": row.get("currency", "").upper() or BASE_CURRENCY,
            "memo": row.get("notes") or row.get("description", ""),
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
            if any("counterparty" in h for h in hl) or any(
                "transaction" in h for h in hl
            ):
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
        # Case-insensitive lookup so headers like "Date"/"Payee"/"Amount" work too
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


# --------------------------------------------------------------
# 4. Account matcher (history + name + currency)
# --------------------------------------------------------------
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
        """Return True if acct.currency is ISO4217 `code` (case-insensitive)"""
        if not getattr(acct, "currency", None):
            return False
        return (
            acct.currency.namespace == "ISO4217"
            and acct.currency.name.upper() == code.upper()
        )

    def find_matching_accounts(
        self,
        payee: str,
        description: str = "",
        transaction_currency: str = BASE_CURRENCY,
        max_suggestions: int = MAX_ACCOUNT_SUGGESTIONS,
    ) -> List[Tuple[object, float, str]]:
        """Return up to max_suggestions [(account, confidence, reason), ...]
        only for accounts whose currency matches."""
        all_sug = []

        # 1. manual mapping - only if currency matches
        mm = self.payee_mapper.get_mapping(payee)
        if mm:
            acct = self.get_account_by_guid(mm["account_guid"])
            if acct and self._acct_currency_matches(acct, transaction_currency):
                all_sug.append(
                    (acct, 1.0, f"Manual mapping (used {mm.get('use_count', 1)} times)")
                )

        # 2. suggestions from transaction history - filter by currency
        for acct_full, conf, reason in self.history_analyzer.suggest_mapping(
            payee, description
        ):
            acct = self._find_account_by_fullname(acct_full)
            if acct and self._acct_currency_matches(acct, transaction_currency):
                all_sug.append((acct, conf, f"History: {reason}"))

        # 3. simple name-based matching - only consider accounts with right currency
        payee_l = payee.lower().strip()
        for acct in self.accounts_cache.values():
            if not self._acct_currency_matches(acct, transaction_currency):
                continue  # skip accounts with wrong currency
            an_l = acct.name.lower().strip()
            fn_l = acct.fullname.lower().strip()
            if payee_l == an_l or payee_l == fn_l:
                all_sug.append((acct, 0.95, "Exact account name match"))
            elif payee_l in an_l or payee_l in fn_l:
                all_sug.append((acct, 0.8, "Account name contains payee"))
            elif an_l.find(payee_l) != -1 or fn_l.find(payee_l) != -1:
                all_sug.append((acct, 0.7, "Payee contained in account name"))

        # de-duplicate & sort by confidence (highest first)
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
            for w in ["shell", "bp", "esso", "petrol", "fuel", "transport", "uber", "train", "bus"]
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

    def get_account_by_guid(self, guid: str) -> Optional[object]:
        return self.accounts_cache.get(guid)

    def get_account_guid(self, name: str) -> Optional[str]:
        for acct in self.accounts_cache.values():
            if acct.name == name or acct.fullname == name:
                return acct.guid
        return None


# --------------------------------------------------------------
# 5. Main importer class
# --------------------------------------------------------------
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

    # ----- commodity handling -------------------------------------------------
    def _get_or_create_commodity(self, code: str) -> piecash.Commodity:
        """Return existing ISO4217 commodity or create it if missing."""
        code = code.upper()
        if code not in SUPPORTED_CURRENCIES:
            print(f"{code} not supported - falling back to {BASE_CURRENCY}")
            code = BASE_CURRENCY
        for c in self.book.commodities:
            if c.namespace == "ISO4217" and c.name == code:
                return c
        # create new
        new_c = piecash.Commodity(
            name=code,
            namespace="ISO4217",
            mnemonic=code,
            fullname=f"{code} Currency",
            quote_source="Manual",
        )
        self.book.commodities.append(new_c)
        print(f"Created new currency commodity: {code}")
        return new_c

    # ----- open GnuCash book (SQLite via piecash) -----------------------------
    def open_book(self, readonly: bool = True):
        """Open the GnuCash SQLite book via piecash."""
        self.gnucash_file = os.path.abspath(self.gnucash_file)
        if not os.path.exists(self.gnucash_file):
            raise FileNotFoundError(f"GnuCash file not found: {self.gnucash_file}")

        self.book = piecash.open_book(
            self.gnucash_file,
            readonly=readonly,
            open_if_lock=True,
        )

    # ----- export account tree to JSON ----------------------------------------
    def export_accounts_json(self, json_path: str = ACCOUNTS_EXPORT_FILE):
        """Dump every account's guid/name/fullname/type/description/currency to JSON."""
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

    # ----- read CSV -----------------------------------------------------------
    def read_csv(self) -> List[dict]:
        if not os.path.exists(self.csv_file):
            raise FileNotFoundError(f"CSV not found: {self.csv_file}")
        fmt = self.mapper.detect_format(self.csv_file)
        print(f"Detected bank format: {fmt}")

        rows = []
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
                    m["row_number"] = i
                    m["import_hash"] = self._tx_hash(m)
                    rows.append(m)
                except Exception as e:
                    print(f"Row {i}: mapping error - {e}")
        print(f"Read {len(rows)} transactions")
        return rows

    # ----- main processing loop ---------------------------------------------
    def process_transactions(self, transactions: List[dict]):
        print("\n" + "=" * 70)
        print("TRANSACTION PROCESSING")
        print("=" * 70)

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

            # duplicate check
            if tx["import_hash"] in self.imported_tx:
                print("  Already imported - skipping")
                continue

            # 1. Try persistent mapping (if currency matches)
            mm = self.payee_mapper.get_mapping(tx["payee"])
            if mm:
                acct = self.matcher.get_account_by_guid(mm["account_guid"])
                if acct and self.matcher._acct_currency_matches(acct, tx["currency"]):
                    print(
                        f"\n  MAPPED: {mm['account_fullname']} "
                        f"(used {mm.get('use_count', 1)} times)"
                    )
                    if not self.dry_run:
                        ans = input("  Use this mapping? [Y]/n/edit: ").strip().lower()
                        if ans in ["", "y", "yes"]:
                            self.payee_mapper.update_last_used(tx["payee"])
                            self._prepare_tx(acct, tx)
                            continue
                        elif ans == "edit":
                            pass  # fall through to manual selection
                    else:
                        print("  [DRY RUN] Would use existing mapping")
                        self._prepare_tx(acct, tx)
                        continue
                else:
                    print(
                        f"  Mapping exists but currency mismatch or account gone: "
                        f"{mm['account_fullname']}"
                    )

            # 2. Get history-based suggestions (filtered by currency)
            sugg = self.matcher.find_matching_accounts(
                tx["payee"], tx.get("memo", ""), transaction_currency=tx["currency"]
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

            # 3. Manual selection
            if self.dry_run:
                print("  [DRY RUN] Skipping manual selection")
                continue

            sel = self._manual_sel(tx, sugg)
            if sel:
                self._prepare_tx(sel, tx)
            else:
                print("  Transaction skipped")

    # ----- manual selection UI ------------------------------------------------
    def _manual_sel(
        self, tx: dict, sugg: List[Tuple[object, float, str]]
    ) -> Optional[object]:
        print("\n  Account Selection:")
        for i, (ac, conf, reason) in enumerate(sugg, 1):
            print(f"   {i}. {ac.fullname} ({conf:.0%} confidence)")
        cat = self.matcher.suggest_category(tx["payee"])
        print(f"   {len(sugg) + 1}. Create new account (suggested: {cat})")
        print(f"   {len(sugg) + 2}. Skip this transaction")
        print(f"   {len(sugg) + 3}. Manage mappings")

        try:
            ch = int(input(f"  Enter choice (1-{len(sugg) + 3}): "))
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
            path = input(f"  New account path (default {cat}): ").strip() or cat
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
            print("  Skipped")
            return None
        if ch == len(sugg) + 3:
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

    # ----- create new account -------------------------------------------------
    def _new_acct(self, path: str, tx: dict) -> Optional[object]:
        parts = path.split(":")
        if len(parts) < 2:
            print("  Path needs at least two parts, e.g. Expenses:Food")
            return None
        parent = None
        cur = ""
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
                print(f"  Created {cur} ({tx['currency']})")
                self.accts_to_create.append(new)
                self.matcher.accounts_cache[new.guid] = new
                parent = new
            except Exception as e:
                print(f"  Could not create {cur}: {e}")
                return None
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

    # ----- prepare a transaction for later creation ---------------------------
    def _prepare_tx(self, acct: object, tx: dict):
        commod = self._get_or_create_commodity(tx["currency"])
        amt = piecash.Money(tx["amount"], commod)
        self.tx_to_create.append(
            {
                "account": acct,
                "date": tx["date"],
                "payee": tx["payee"],
                "amount": amt,
                "memo": tx.get("memo", ""),
                "hash": tx["import_hash"],
            }
        )
        print(f"  Prepared for {acct.fullname} ({tx['currency']})")

    # ----- execute the import -------------------------------------------------
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
                tx = piecash.Transaction(
                    currency=info["amount"].currency,
                    description=info["payee"][:250],
                    splits=[
                        piecash.Split(
                            account=info["account"],
                            value=info["amount"],
                            memo=info["memo"][:200],
                        )
                    ],
                )
                self.book.add(tx)
                self.imported_tx[info["hash"]] = {
                    "timestamp": datetime.now().isoformat(),
                    "payee": info["payee"],
                    "amount": str(info["amount"]),
                    "currency": info["amount"].currency.mnemonic,
                    "account": info["account"].fullname,
                }
                created += 1
                print(
                    f"  {info['payee'][:50]} - {abs(info['amount']):.2f} {info['amount'].currency.mnemonic}"
                )
            except Exception as e:
                print(f"  Failed {info['payee']}: {e}")

        if created:
            self.book.save()
            self._save_imported()
            print(f"\n{created} transactions saved to {self.gnucash_file}")
        else:
            print("\nNo transactions created")


# --------------------------------------------------------------
# 6. CLI entry point
# --------------------------------------------------------------
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
        "--dry-run", action="store_true", help="Show what would be done without saving"
    )
    parser.add_argument(
        "--auto-accept",
        action="store_true",
        help="Auto-accept first suggestion for every row",
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

    try:
        imp = TransactionImporter(args.gnucash_file, args.csv_file)
        imp.dry_run = args.dry_run
        imp.payee_mapper = payee_mapper

        imp.open_book(readonly=not args.dry_run and not args.auto_accept)

        if args.export_accounts:
            imp.export_accounts_json()
            imp.book.close()
            return

        # Build the history analyzer + account matcher BEFORE processing rows --
        # these were previously left as None, causing an AttributeError.
        imp.history_analyzer = TransactionHistoryAnalyzer(imp.book)
        imp.history_analyzer.analyze_transactions()
        imp.matcher = AccountMatcher(imp.book, imp.payee_mapper, imp.history_analyzer)

        rows = imp.read_csv()

        if not rows:
            print("No transactions in CSV")
            imp.book.close()
            return

        imp.process_transactions(rows)

        if imp.tx_to_create:
            print(f"\nPrepared {len(imp.tx_to_create)} transactions for import")
            if not args.dry_run:
                if input("\nExecute import now? [y/N]: ").strip().lower() == "y":
                    imp.execute_import()
                else:
                    print("Import cancelled")
        else:
            print("\n[DRY RUN] Run again without --dry-run to execute")

        imp.book.close()

    except KeyboardInterrupt:
        print("\nCancelled by user")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
