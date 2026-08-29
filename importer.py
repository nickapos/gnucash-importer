"""Core GnuCash import workflow and duplicate detection."""

import hashlib
import json
import os
import shutil
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Optional
import difflib

import piecash

from bank_formats import read_bank_csv
from config import (
    ACCOUNTS_EXPORT_FILE,
    ASSET_LIKE_TYPES,
    DEFAULT_KEEP_BACKUPS,
    DEFAULT_PENDING_DUPLICATE_WINDOW_DAYS,
    DUPLICATE_CHECK_FILE,
    SUPPORTED_CURRENCIES,
)
from utils import (
    AccountPathCompleter,
    input_with_completion,
    is_real_asset_account,
    normalize_payee,
    parse_tx_date,
)


class TransactionImporter:
    def __init__(self, gnucash_file: str, csv_file: str):
        self.gnucash_file = os.path.abspath(gnucash_file)
        self.csv_file = csv_file
        self.book = None
        self.matcher = None
        self.history_analyzer = None
        self.payee_mapper = None
        self.source_account = None
        self.dry_run = False
        self.auto_accept = False
        self.include_pending = False
        self.pending_duplicate_window_days = DEFAULT_PENDING_DUPLICATE_WINDOW_DAYS
        self.keep_backups = DEFAULT_KEEP_BACKUPS
        self.check_ledger = True
        self._backup_created = False
        self._existing_ledger_index = None
        self.tx_to_create = []
        self.imported_tx = self._load_imported()

    def _load_imported(self) -> dict:
        if not os.path.exists(DUPLICATE_CHECK_FILE):
            return {}
        try:
            with open(DUPLICATE_CHECK_FILE, "r") as handle:
                return json.load(handle)
        except Exception:
            return {}

    def _save_imported(self) -> None:
        with open(DUPLICATE_CHECK_FILE, "w") as handle:
            json.dump(self.imported_tx, handle, indent=2)

    @staticmethod
    def tx_hash(tx: dict) -> str:
        raw = f"{tx['date']}|{tx['payee']}|{tx['amount']:.2f}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def open_book(self, readonly: bool) -> None:
        if not os.path.exists(self.gnucash_file):
            raise FileNotFoundError(f"GnuCash file not found: {self.gnucash_file}")
        self.book = piecash.open_book(
            self.gnucash_file,
            readonly=readonly,
            open_if_lock=True,
        )

    def close_book(self) -> None:
        if self.book is not None:
            self.book.close()
            print("GnuCash book closed - lock released.")

    def read_csv(self) -> list[dict]:
        fmt, rows = read_bank_csv(self.csv_file, include_pending=self.include_pending)
        for row in rows:
            row["import_hash"] = self.tx_hash(row)
        print(f"Detected bank format: {fmt}")
        print(f"Read {len(rows)} transactions")
        return rows

    def resolve_source_account(self, path_hint: Optional[str] = None):
        if path_hint:
            guid = self.matcher.get_account_guid(path_hint)
            if guid:
                account = self.matcher.get_account_by_guid(guid)
                print(f"Using source account: {account.fullname}")
                return account
            print(f"Warning: source account '{path_hint}' not found.")

        all_accounts = [
            account
            for account in self.matcher.accounts_cache.values()
            if account.type in ASSET_LIKE_TYPES and is_real_asset_account(account)
        ]
        postable = sorted(
            (account for account in all_accounts if account.placeholder == 0),
            key=lambda account: account.fullname,
        )
        print(f"\nFound {len(all_accounts)} asset-like accounts ({len(postable)} postable).")
        for index, account in enumerate(postable, 1):
            currency = account.commodity.mnemonic if account.commodity else "?"
            print(f"  {index}. {account.fullname} [{account.type}] ({currency})")
        print("  Enter an index, exact account path, or search term.")

        completer = AccountPathCompleter([account.fullname for account in postable])
        while True:
            choice = input_with_completion("Source account: ", completer).strip()
            if choice.isdigit() and 1 <= int(choice) <= len(postable):
                return postable[int(choice) - 1]
            guid = self.matcher.get_account_guid(choice)
            if guid:
                account = self.matcher.get_account_by_guid(guid)
                if account.placeholder == 0:
                    return account
                print("That account is a placeholder and cannot hold transactions.")
                continue
            matches = [account for account in all_accounts if choice.lower() in account.fullname.lower()]
            if not matches:
                print("No matching source accounts found.")
                continue
            for index, account in enumerate(matches, 1):
                marker = " (placeholder)" if account.placeholder != 0 else ""
                currency = account.commodity.mnemonic if account.commodity else "?"
                print(f"  {index}. {account.fullname} [{account.type}] ({currency}){marker}")
            selected = input("Select matching account number, or Enter to search again: ").strip()
            if selected.isdigit() and 1 <= int(selected) <= len(matches):
                account = matches[int(selected) - 1]
                if account.placeholder == 0:
                    return account
                print("That account is a placeholder and cannot hold transactions.")

    def _backup_directory(self) -> str:
        return os.path.join(os.path.dirname(self.gnucash_file), "backups")

    def _create_database_backup(self) -> None:
        if self.dry_run or getattr(self, "_backup_created", False):
            return
        backup_dir = self._backup_directory()
        os.makedirs(backup_dir, exist_ok=True)
        stem, extension = os.path.splitext(os.path.basename(self.gnucash_file))
        extension = extension or ".gnucash"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(backup_dir, f"{stem}.backup-{stamp}{extension}")
        shutil.copy2(self.gnucash_file, backup_path)
        self._backup_created = True
        print(f"Created database backup: {backup_path}")
        self._prune_backups(backup_dir, stem, extension)

    def _prune_backups(self, directory: str, stem: str, extension: str) -> None:
        if self.keep_backups <= 0:
            return
        prefix = f"{stem}.backup-"
        backups = sorted(
            (
                os.path.join(directory, filename)
                for filename in os.listdir(directory)
                if filename.startswith(prefix) and filename.endswith(extension)
            ),
            key=os.path.getmtime,
            reverse=True,
        )
        for old_backup in backups[self.keep_backups:]:
            os.remove(old_backup)
            print(f"Deleted old backup: {old_backup}")

    def _get_or_create_commodity(self, code: str):
        code = code.upper()
        if code not in SUPPORTED_CURRENCIES:
            code = "GBP"
        for commodity in self.book.commodities:
            if commodity.namespace == "CURRENCY" and commodity.mnemonic == code:
                return commodity
        commodity = piecash.Commodity(
            name=code,
            namespace="CURRENCY",
            mnemonic=code,
            fullname=f"{code} Currency",
            quote_source="Manual",
        )
        self.book.commodities.append(commodity)
        return commodity

    def _build_ledger_index(self) -> None:
        index = defaultdict(list)
        for txn in self.book.transactions:
            for split in txn.splits or []:
                if split.account and split.account.guid == self.source_account.guid:
                    index[(txn.post_date, round(float(split.value), 2))].append(txn)
        self._existing_ledger_index = index
        print(f"Indexed {sum(len(v) for v in index.values())} source-account ledger entries")

    def _exact_ledger_duplicate(self, tx: dict):
        if not self._existing_ledger_index:
            return None
        key = (parse_tx_date(tx["date"]).date(), round(float(tx["amount"]), 2))
        candidates = self._existing_ledger_index.get(key, [])
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: difflib.SequenceMatcher(
                None,
                normalize_payee(tx["payee"]),
                normalize_payee(item.description or ""),
            ).ratio(),
        )

    def _pending_ledger_duplicate(self, tx: dict):
        if not tx.get("is_pending"):
            return None
        pending_date = parse_tx_date(tx["date"]).date()
        amount = round(float(tx["amount"]), 2)
        payee = normalize_payee(tx["payee"])
        best = None
        best_score = 0.0

        for txn in self.book.transactions:
            if not txn.post_date:
                continue
            if abs((txn.post_date - pending_date).days) > self.pending_duplicate_window_days:
                continue
            source_split = next(
                (
                    split
                    for split in (txn.splits or [])
                    if split.account and split.account.guid == self.source_account.guid
                ),
                None,
            )
            if source_split is None or round(float(source_split.value), 2) != amount:
                continue
            score = difflib.SequenceMatcher(
                None,
                payee,
                normalize_payee(txn.description or ""),
            ).ratio()
            if score > best_score:
                best = txn
                best_score = score

        if best is None or best_score < 0.55:
            return None
        return best, best_score

    def _mark_skipped(self, tx: dict, reason: str) -> None:
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
        print(f"  Marked skipped/imported: {tx['payee']}")

    def _pending_duplicate_decision(self, tx: dict) -> bool:
        result = self._pending_ledger_duplicate(tx)
        if result is None:
            return False
        txn, similarity = result
        print("\n  POSSIBLE PENDING-TO-COMPLETED DUPLICATE")
        print(f"    Pending CSV row: {tx['date']} | {tx['payee']} | {tx['amount']:.2f} {tx['currency']}")
        print(f"    Existing ledger: {txn.post_date} | {txn.description!r} | same source amount")
        print(f"    Payee similarity: {similarity:.0%}; date window: ±{self.pending_duplicate_window_days} day(s)")

        if self.dry_run:
            print("  [DRY RUN] Would treat this as already handled")
            return True
        if self.auto_accept:
            self._mark_skipped(tx, "pending row matched a posted ledger transaction")
            return True
        answer = input("  Is this the same completed transaction? [Y]es (skip) / n (import pending anyway): ").strip().lower()
        if answer in {"", "y", "yes"}:
            self._mark_skipped(tx, "pending row matched a posted ledger transaction (user confirmed)")
            return True
        return False

    def _prepare_tx(self, destination, tx: dict) -> None:
        self.tx_to_create.append(
            {
                "source_account": self.source_account,
                "dest_account": destination,
                "date": tx["date"],
                "payee": tx["payee"],
                "amount": Decimal(str(tx["amount"])),
                "commodity": self._get_or_create_commodity(tx["currency"]),
                "memo": tx.get("memo", ""),
                "hash": tx["import_hash"],
            }
        )
        print(f"  Prepared: {self.source_account.fullname} <-> {destination.fullname}")

    def process_transactions(self, transactions: list[dict]) -> None:
        if self.check_ledger:
            self._build_ledger_index()

        for index, tx in enumerate(transactions, 1):
            print(f"\n[{index}/{len(transactions)}] {tx['date']} | {tx['payee']} | {tx['amount']:.2f} {tx['currency']}")
            if tx.get("is_pending"):
                print("  Status: PENDING")
            if tx["import_hash"] in self.imported_tx:
                print("  Already handled by importer - skipping")
                continue
            if tx.get("is_pending") and self._pending_duplicate_decision(tx):
                continue
            if self.check_ledger:
                duplicate = self._exact_ledger_duplicate(tx)
                if duplicate is not None:
                    print(f"  POSSIBLE DUPLICATE: {duplicate.post_date} | {duplicate.description!r}")
                    if self.dry_run:
                        continue
                    answer = input("  Is this the same transaction? [Y]es (skip) / n (import anyway): ").strip().lower()
                    if answer in {"", "y", "yes"}:
                        self._mark_skipped(tx, "matched existing ledger transaction")
                        continue

            mapping = self.payee_mapper.get_mapping(tx["payee"])
            if mapping:
                destination = self.matcher.get_account_by_guid(mapping["account_guid"])
                if destination and self.matcher._acct_currency_matches(destination, tx["currency"]):
                    print(f"  MAPPED: {destination.fullname}")
                    if self.dry_run or self.auto_accept:
                        self._prepare_tx(destination, tx)
                        continue
                    answer = input("  Use mapping? [Y]/n/edit: ").strip().lower()
                    if answer in {"", "y", "yes"}:
                        self.payee_mapper.update_last_used(tx["payee"])
                        self._prepare_tx(destination, tx)
                        continue

            suggestions = self.matcher.find_matching_accounts(
                tx["payee"],
                tx.get("memo", ""),
                tx["currency"],
                exclude_guid=self.source_account.guid,
            )
            if self.dry_run:
                if suggestions:
                    for account, confidence, reason in suggestions:
                        print(f"  Suggestion: {account.fullname} ({confidence:.0%}) — {reason}")
                else:
                    print(f"  No suggestion. New-account category fallback: {self.matcher.suggest_category(tx['payee'])}")
                continue

            destination = self._select_account(tx, suggestions)
            if destination:
                self._prepare_tx(destination, tx)

    def _suggest_new_account_path(self, tx: dict, suggestions: list) -> str:
        if suggestions:
            parts = suggestions[0][0].fullname.split(":")
            if len(parts) >= 2:
                return f"{':'.join(parts[:2])}:{tx['payee']}"
        return f"{self.matcher.suggest_category(tx['payee'])}:{tx['payee']}"

    def _create_account_path(self, path: str, tx: dict):
        parts = [part.strip() for part in path.split(":") if part.strip()]
        if len(parts) < 2:
            print("  Account path needs at least two levels, e.g. Expenses:Travel")
            return None

        parent = None
        current_path = ""
        created = False
        for part in parts:
            current_path = f"{current_path}:{part}" if current_path else part
            existing_guid = self.matcher.get_account_guid(current_path)
            if existing_guid:
                parent = self.matcher.get_account_by_guid(existing_guid)
                continue

            self._create_database_backup()
            account = piecash.Account(
                name=part,
                type=self._infer_account_type(current_path),
                parent=parent if parent else self.book.root_account,
                commodity=self._get_or_create_commodity(tx["currency"]),
            )
            self.book.add(account)
            self.matcher.accounts_cache[account.guid] = account
            parent = account
            created = True
            print(f"  Created account: {current_path} ({tx['currency']})")

        if created:
            self.book.save()
            print(f"  Saved new account structure to {self.gnucash_file}")
        return parent

    @staticmethod
    def _infer_account_type(path: str) -> str:
        root = path.split(":", 1)[0].lower()
        return {
            "income": "INCOME",
            "expenses": "EXPENSE",
            "expense": "EXPENSE",
            "assets": "ASSET",
            "asset": "ASSET",
            "liabilities": "LIABILITY",
            "liability": "LIABILITY",
            "equity": "EQUITY",
        }.get(root, "EXPENSE")

    def _account_path_completer(self) -> AccountPathCompleter:
        return AccountPathCompleter(
            [account.fullname for account in self.matcher.accounts_cache.values()]
        )

    def _new_account_path_prompt(self, suggested_path: str) -> str:
        """Prompt for a new account path with the same tab completion as
        manual account entry. Tab completes the existing account prefix;
        after completing a parent path, append ':New Account Name'.

        Example:
          Type 'Expenses:Trav', press Tab, then append ':Eurowings'.
        """
        return input_with_completion(
            f"  New account path (default {suggested_path}; Tab completes existing paths): ",
            self._account_path_completer(),
        ).strip() or suggested_path

    def _manual_account_path(self, tx: dict):
        path = input_with_completion(
            "  Existing or new account path (Tab to autocomplete): ",
            self._account_path_completer(),
        ).strip()
        if not path:
            return None
        guid = self.matcher.get_account_guid(path)
        if guid:
            account = self.matcher.get_account_by_guid(guid)
            print(f"  Using existing account: {account.fullname}")
            return account
        return self._create_account_path(path, tx)

    def _offer_mapping_save(self, tx: dict, account) -> None:
        if account and input("  Save payee mapping? [Y]/n: ").strip().lower() in {"", "y", "yes"}:
            self.payee_mapper.add_mapping(tx["payee"], account.fullname, account.guid)
            print(f"  Saved mapping: '{tx['payee']}' -> '{account.fullname}'")

    def _select_account(self, tx: dict, suggestions: list):
        print("\n  Account Selection:")
        for index, (account, confidence, reason) in enumerate(suggestions, 1):
            print(f"  {index}. {account.fullname} ({confidence:.0%}) — {reason}")

        new_path = self._suggest_new_account_path(tx, suggestions)
        create_choice = len(suggestions) + 1
        manual_choice = len(suggestions) + 2
        permanent_skip_choice = len(suggestions) + 3
        temporary_skip_choice = len(suggestions) + 4

        print(f"  {create_choice}. Create new account (suggested: {new_path})")
        print(f"  {manual_choice}. Enter existing or new account path manually (Tab to autocomplete)")
        print(f"  {permanent_skip_choice}. Skip and mark as imported (never ask again)")
        print(f"  {temporary_skip_choice}. Skip for now (ask again next run)")

        try:
            choice = int(input(f"  Choice (1-{temporary_skip_choice}): "))
        except ValueError:
            print("  Invalid selection - skipping for now")
            return None

        if 1 <= choice <= len(suggestions):
            account = suggestions[choice - 1][0]
            self._offer_mapping_save(tx, account)
            return account

        if choice == create_choice:
            # This is intentionally completion-enabled, unlike the previous
            # implementation that used plain input() at this exact prompt.
            path = self._new_account_path_prompt(new_path)
            account = self._create_account_path(path, tx)
            self._offer_mapping_save(tx, account)
            return account

        if choice == manual_choice:
            account = self._manual_account_path(tx)
            self._offer_mapping_save(tx, account)
            return account

        if choice == permanent_skip_choice:
            self._mark_skipped(tx, "user skipped and marked as imported")
            return None

        if choice == temporary_skip_choice:
            print("  Skipped for now (will be shown again next run)")
            return None

        print("  Invalid selection - skipping for now")
        return None

    def execute_import(self) -> None:
        if not self.tx_to_create:
            print("No transactions to import")
            return

        self._create_database_backup()
        for info in self.tx_to_create:
            post_date = parse_tx_date(info["date"])
            amount = info["amount"]
            transaction = piecash.Transaction(
                currency=info["commodity"],
                description=info["payee"][:250],
                post_date=post_date.date(),
                enter_date=datetime.now(),
                splits=[
                    piecash.Split(
                        account=info["source_account"],
                        value=amount,
                        memo=info["memo"][:200],
                    ),
                    piecash.Split(
                        account=info["dest_account"],
                        value=-amount,
                        memo=info["memo"][:200],
                    ),
                ],
            )
            self.book.add(transaction)
            self.imported_tx[info["hash"]] = {
                "timestamp": datetime.now().isoformat(),
                "payee": info["payee"],
                "amount": str(amount),
                "currency": info["commodity"].mnemonic,
                "source_account": info["source_account"].fullname,
                "dest_account": info["dest_account"].fullname,
                "csv_date": info["date"],
                "tx_guid": transaction.guid,
            }

        self.book.save()
        self._save_imported()
        print(f"Saved {len(self.tx_to_create)} transactions")
