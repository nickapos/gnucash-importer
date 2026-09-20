"""Tests for importer.py — core GnuCash import workflow (piecash-dependent).

These tests mock the piecash ORM layer, so no real GnuCash book is touched.
piecash is only installed in the project virtualenv, therefore this module is
skipped automatically under the system python (``python3``) via importorskip.

Run the full suite with:

    .venv/bin/python -m pytest tests/ -q
"""
import json
import os
from datetime import date
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("piecash")

import importer as importer_module
from config import (
    EXACT_DUPLICATE_MIN_SIMILARITY,
    PENDING_DUPLICATE_MIN_SIMILARITY,
)
from importer import DuplicateMatch, TransactionImporter
from utils import compute_tx_hash


class TestTransactionImporterHarness:
    """Shared helpers for exercising TransactionImporter."""

    @staticmethod
    def _make_importer(monkeypatch, tmp_path, gnucash_file="dummy.gnucash"):
        """Construct a TransactionImporter isolated from the repo state.

        ``DUPLICATE_CHECK_FILE`` is redirected into the tmp dir so tests never
        read or write the real ``.imported_transactions.json`` in the repo.
        """
        monkeypatch.setattr(
            importer_module,
            "DUPLICATE_CHECK_FILE",
            os.path.join(str(tmp_path), ".imported_transactions.json"),
        )
        imp = TransactionImporter(gnucash_file, "dummy.csv")
        imp.dry_run = True
        return imp

    @staticmethod
    def _make_account(fullname="Assets:Bank", type_="BANK", placeholder=0,
                      currency="GBP", guid=None):
        account = Mock()
        account.fullname = fullname
        account.name = fullname.split(":")[-1]
        account.guid = guid or f"guid:{fullname}"
        account.type = type_
        account.placeholder = placeholder
        commodity = Mock()
        commodity.namespace = "CURRENCY"
        commodity.mnemonic = currency
        account.commodity = commodity
        return account

    @staticmethod
    def _make_txn(description, post_date, splits, guid="txn-1"):
        txn = Mock()
        txn.description = description
        txn.post_date = post_date
        txn.enter_date = None
        txn.guid = guid
        txn.splits = splits
        return txn

    @staticmethod
    def _make_split(account, value):
        split = Mock()
        split.account = account
        split.value = value
        return split

    @staticmethod
    def _make_tx(payee="Tesco Store", amount=-25.5, currency="GBP",
                 date_str="2026-01-15", is_pending=False, hash_=None, memo=""):
        return {
            "date": date_str,
            "payee": payee,
            "amount": amount,
            "currency": currency,
            "memo": memo,
            "is_pending": is_pending,
            "import_hash": hash_ or f"hash-{payee}-{amount}",
        }


class TestBasics(TestTransactionImporterHarness):
    """Hashing, state files, book open/close, and account export."""

    def test_tx_hash_stable(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        tx = {"date": "2026-01-15", "payee": "Tesco Store", "amount": -25.5}
        assert imp.tx_hash(tx) == imp.tx_hash(dict(tx))
        changed = dict(tx)
        changed["amount"] = -26.0
        assert imp.tx_hash(tx) != imp.tx_hash(changed)

    def test_tx_hash_matches_utils_shared_implementation(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        tx = {"date": "2026-01-15", "payee": "Tesco Store", "amount": -25.5}
        # The validator must reproduce exactly the same hash.
        assert imp.tx_hash(tx) == compute_tx_hash(tx)

    def test_load_imported_missing_file(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        assert imp.imported_tx == {}

    def test_load_imported_valid_json(self, monkeypatch, tmp_path):
        path = os.path.join(str(tmp_path), ".imported_transactions.json")
        with open(path, "w") as handle:
            json.dump({"h1": {"skipped": True}}, handle)
        monkeypatch.setattr(importer_module, "DUPLICATE_CHECK_FILE", path)
        imp = TransactionImporter("dummy.gnucash", "dummy.csv")
        assert imp.imported_tx == {"h1": {"skipped": True}}

    def test_load_imported_corrupt_json_returns_empty(self, monkeypatch, tmp_path, capsys):
        path = os.path.join(str(tmp_path), ".imported_transactions.json")
        with open(path, "w") as handle:
            handle.write("{not valid json")
        monkeypatch.setattr(importer_module, "DUPLICATE_CHECK_FILE", path)
        imp = TransactionImporter("dummy.gnucash", "dummy.csv")
        assert imp.imported_tx == {}
        assert "Warning" in capsys.readouterr().out

    def test_save_imported_roundtrip(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.imported_tx = {"h1": {"skipped": True, "payee": "Tesco"}}
        imp._save_imported()
        with open(os.path.join(str(tmp_path), ".imported_transactions.json")) as handle:
            assert json.load(handle) == imp.imported_tx

    def test_open_book_missing_file_raises(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        with patch.object(importer_module.piecash, "open_book") as open_mock:
            with pytest.raises(FileNotFoundError):
                imp.open_book(readonly=True)
        open_mock.assert_not_called()

    def test_open_book_calls_piecash(self, monkeypatch, tmp_path):
        book_path = os.path.join(str(tmp_path), "ledger.gnucash")
        with open(book_path, "w") as handle:
            handle.write("sqlite content")
        imp = self._make_importer(monkeypatch, tmp_path, gnucash_file=book_path)
        book = Mock()
        with patch.object(importer_module.piecash, "open_book", return_value=book) as open_mock:
            imp.open_book(readonly=True)
        open_mock.assert_called_once_with(book_path, readonly=True, open_if_lock=True)
        assert imp.book is book

    def test_close_book_releases(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        book = Mock()
        imp.book = book
        imp.close_book()
        book.close.assert_called_once()
        imp.book = None
        imp.close_book()  # must not raise

    def test_export_accounts_json_sorted(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        zeta = self._make_account("Assets:Zeta")
        alpha = self._make_account("Assets:Alpha")
        imp.book = Mock()
        imp.book.accounts = [zeta, alpha]
        with patch.object(importer_module, "atomic_write_json") as writer:
            imp.export_accounts_json("out.json")
        payload = writer.call_args.args[1]
        assert [item["fullname"] for item in payload] == ["Assets:Alpha", "Assets:Zeta"]
        assert payload[0]["currency"] == "GBP"
        assert payload[0]["placeholder"] is False
        assert payload[0]["guid"] == alpha.guid
        assert writer.call_args.args[0] == "out.json"

    def test_read_csv_adds_import_hashes(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        row = {"date": "2026-01-15", "payee": "Tesco", "amount": -25.5, "currency": "GBP"}
        with patch.object(
            importer_module, "read_bank_csv", return_value=("generic", [dict(row)])
        ) as reader_mock:
            rows = imp.read_csv()
        assert len(rows) == 1
        assert rows[0]["import_hash"] == compute_tx_hash(row)
        reader_mock.assert_called_once_with("dummy.csv", include_pending=False)


class TestCommodity(TestTransactionImporterHarness):
    """_get_or_create_commodity resolves or creates currency commodities."""

    def test_existing_commodity_returned(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        gbp = Mock()
        gbp.namespace = "CURRENCY"
        gbp.mnemonic = "GBP"
        usd = Mock()
        usd.namespace = "CURRENCY"
        usd.mnemonic = "USD"
        imp.book = Mock()
        imp.book.commodities = [gbp, usd]
        assert imp._get_or_create_commodity("gbp") is gbp

    def test_new_commodity_created_and_appended(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.book = Mock()
        imp.book.commodities = []
        new_commodity = Mock()
        with patch.object(
            importer_module.piecash, "Commodity", return_value=new_commodity
        ) as commodity_cls:
            result = imp._get_or_create_commodity("EUR")
        assert result is new_commodity
        assert imp.book.commodities == [new_commodity]
        assert commodity_cls.call_args.kwargs["mnemonic"] == "EUR"
        assert commodity_cls.call_args.kwargs["namespace"] == "CURRENCY"

    def test_unsupported_currency_falls_back_to_gbp(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.book = Mock()
        imp.book.commodities = []
        with patch.object(
            importer_module.piecash, "Commodity", return_value=Mock()
        ) as commodity_cls:
            imp._get_or_create_commodity("JPY")
        assert commodity_cls.call_args.kwargs["mnemonic"] == "GBP"


class TestLedgerDuplicate(TestTransactionImporterHarness):
    """Ledger indexing and exact/pending duplicate detection."""

    def test_build_ledger_index(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        src = self._make_account("Assets:Bank")
        imp.source_account = src
        txn1 = self._make_txn("Tesco", date(2026, 1, 15), [self._make_split(src, -25.5)])
        other = self._make_account("Expenses:Food")
        txn2 = self._make_txn("Salary", date(2026, 1, 16), [self._make_split(other, 1500.0)])
        imp.book = Mock()
        imp.book.transactions = [txn1, txn2]
        imp._build_ledger_index()
        assert imp._existing_ledger_index[(date(2026, 1, 15), -25.5)] == [txn1]
        assert imp._pending_ledger_index[-25.5] == [txn1]

    def test_exact_duplicate_match(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        existing = self._make_txn("TESCO STORES", date(2026, 1, 15), [])
        imp._existing_ledger_index = {(date(2026, 1, 15), -25.5): [existing]}
        imp._pending_ledger_index = {}
        match = imp._exact_ledger_duplicate(
            {"date": "2026-01-15", "amount": -25.5, "payee": "Tesco Stores"}
        )
        assert match is not None
        assert match.similarity >= EXACT_DUPLICATE_MIN_SIMILARITY

    def test_exact_duplicate_low_similarity_returns_none(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        existing = self._make_txn("TESCO STORES", date(2026, 1, 15), [])
        imp._existing_ledger_index = {(date(2026, 1, 15), -25.5): [existing]}
        imp._pending_ledger_index = {}
        match = imp._exact_ledger_duplicate(
            {"date": "2026-01-15", "amount": -25.5, "payee": "Netflix Subscription"}
        )
        assert match is None

    def test_exact_duplicate_no_index_returns_none(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp._existing_ledger_index = {}
        assert imp._exact_ledger_duplicate(
            {"date": "2026-01-15", "amount": -25.5, "payee": "Tesco"}
        ) is None

    def test_pending_duplicate_requires_pending_flag(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        tx = self._make_tx(is_pending=False)
        assert imp._pending_ledger_duplicate(tx) is None

    def test_pending_duplicate_requires_source_account(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.source_account = None
        tx = self._make_tx(is_pending=True)
        assert imp._pending_ledger_duplicate(tx) is None

    def test_pending_duplicate_finds_match_in_window(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        src = self._make_account("Assets:Bank")
        imp.source_account = src
        imp.pending_duplicate_window_days = 7
        existing = self._make_txn(
            "Uber Ride London", date(2026, 1, 15), [self._make_split(src, -12.5)]
        )
        imp._pending_ledger_index = {-12.5: [existing]}
        imp.book = Mock()
        tx = self._make_tx(payee="Uber Ride", amount=-12.5, is_pending=True)
        match = imp._pending_ledger_duplicate(tx)
        assert match is not None
        assert match.similarity >= PENDING_DUPLICATE_MIN_SIMILARITY

    def test_pending_duplicate_outside_window(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        src = self._make_account("Assets:Bank")
        imp.source_account = src
        imp.pending_duplicate_window_days = 7
        existing = self._make_txn(
            "Uber Ride London", date(2026, 1, 15), [self._make_split(src, -12.5)]
        )
        imp._pending_ledger_index = {-12.5: [existing]}
        imp.book = Mock()
        tx = self._make_tx(payee="Uber Ride", amount=-12.5, date_str="2026-01-01", is_pending=True)
        assert imp._pending_ledger_duplicate(tx) is None

    def test_pending_duplicate_below_similarity_threshold(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        src = self._make_account("Assets:Bank")
        imp.source_account = src
        imp.pending_duplicate_window_days = 7
        existing = self._make_txn(
            "British Gas Bill", date(2026, 1, 15), [self._make_split(src, -12.5)]
        )
        imp._pending_ledger_index = {-12.5: [existing]}
        imp.book = Mock()
        tx = self._make_tx(payee="Uber Ride", amount=-12.5, is_pending=True)
        assert imp._pending_ledger_duplicate(tx) is None


class TestPendingDecision(TestTransactionImporterHarness):
    """_pending_duplicate_decision handling across modes."""

    def _match(self):
        return DuplicateMatch(txn=Mock(), similarity=0.9)

    def test_dry_run_treats_as_duplicate(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.dry_run = True
        monkeypatch.setattr(imp, "_pending_ledger_duplicate", lambda tx: self._match())
        tx = self._make_tx(is_pending=True)
        assert imp._pending_duplicate_decision(tx) is True
        assert tx["import_hash"] not in imp.imported_tx

    def test_auto_accept_marks_skipped(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.dry_run = False
        imp.auto_accept = True
        monkeypatch.setattr(imp, "_pending_ledger_duplicate", lambda tx: self._match())
        tx = self._make_tx(is_pending=True)
        assert imp._pending_duplicate_decision(tx) is True
        assert imp.imported_tx[tx["import_hash"]]["skipped"] is True

    def test_user_confirms_marks_skipped(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.dry_run = False
        imp.auto_accept = False
        monkeypatch.setattr(imp, "_pending_ledger_duplicate", lambda tx: self._match())
        tx = self._make_tx(is_pending=True)
        with patch("builtins.input", return_value="y"):
            assert imp._pending_duplicate_decision(tx) is True
        assert imp.imported_tx[tx["import_hash"]]["skipped"] is True

    def test_user_declines_imports_anyway(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.dry_run = False
        monkeypatch.setattr(imp, "_pending_ledger_duplicate", lambda tx: self._match())
        tx = self._make_tx(is_pending=True)
        with patch("builtins.input", return_value="n"):
            assert imp._pending_duplicate_decision(tx) is False
        assert tx["import_hash"] not in imp.imported_tx

    def test_no_match_returns_false(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        monkeypatch.setattr(imp, "_pending_ledger_duplicate", lambda tx: None)
        tx = self._make_tx(is_pending=True)
        assert imp._pending_duplicate_decision(tx) is False


class TestMarkAndPrepare(TestTransactionImporterHarness):
    """_mark_skipped and _prepare_tx state mutations."""

    def test_mark_skipped_writes_record(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        tx = self._make_tx()
        imp._mark_skipped(tx, "user skipped")
        record = imp.imported_tx[tx["import_hash"]]
        assert record["skipped"] is True
        assert record["reason"] == "user skipped"
        assert record["payee"] == "Tesco Store"
        with open(os.path.join(str(tmp_path), ".imported_transactions.json")) as handle:
            saved = json.load(handle)
        assert saved[tx["import_hash"]]["skipped"] is True

    def test_prepare_tx_builds_entry(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        src = self._make_account("Assets:Bank")
        dest = self._make_account("Expenses:Food")
        imp.source_account = src
        commodity = Mock()
        commodity.mnemonic = "GBP"
        monkeypatch.setattr(imp, "_get_or_create_commodity", lambda code: commodity)
        tx = self._make_tx(memo="groceries")
        imp._prepare_tx(dest, tx)
        assert len(imp.tx_to_create) == 1
        entry = imp.tx_to_create[0]
        assert entry["source_account"] is src
        assert entry["dest_account"] is dest
        assert entry["amount"] == Decimal("-25.5")
        assert entry["commodity"] is commodity
        assert entry["memo"] == "groceries"
        assert entry["hash"] == tx["import_hash"]


class TestProcessTransactions(TestTransactionImporterHarness):
    """process_transactions end-to-end decision flow."""

    def _setup(self, monkeypatch, tmp_path, dry_run=True, auto_accept=False,
               check_ledger=False):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.dry_run = dry_run
        imp.auto_accept = auto_accept
        imp.check_ledger = check_ledger
        imp.source_account = self._make_account("Assets:Bank")
        imp.book = Mock()
        imp.book.transactions = []
        imp.matcher = Mock()
        imp.payee_mapper = Mock()
        commodity = Mock()
        commodity.mnemonic = "GBP"
        monkeypatch.setattr(imp, "_get_or_create_commodity", lambda code: commodity)
        return imp

    def test_skips_already_handled(self, monkeypatch, tmp_path):
        imp = self._setup(monkeypatch, tmp_path)
        tx = self._make_tx()
        imp.imported_tx = {tx["import_hash"]: {"skipped": True}}
        imp.process_transactions([tx])
        assert imp.tx_to_create == []
        imp.payee_mapper.get_mapping.assert_not_called()

    def test_auto_accept_uses_mapping_and_tracks_usage(self, monkeypatch, tmp_path):
        imp = self._setup(monkeypatch, tmp_path, dry_run=False, auto_accept=True)
        dest = self._make_account("Expenses:Food")
        imp.matcher.get_account_by_guid.return_value = dest
        imp.payee_mapper.get_mapping.return_value = {"account_guid": dest.guid}
        imp.process_transactions([self._make_tx()])
        assert len(imp.tx_to_create) == 1
        imp.payee_mapper.update_last_used.assert_called_once_with("Tesco Store")

    def test_dry_run_mapping_does_not_track_usage(self, monkeypatch, tmp_path):
        imp = self._setup(monkeypatch, tmp_path, dry_run=True)
        dest = self._make_account("Expenses:Food")
        imp.matcher.get_account_by_guid.return_value = dest
        imp.payee_mapper.get_mapping.return_value = {"account_guid": dest.guid}
        imp.process_transactions([self._make_tx()])
        assert len(imp.tx_to_create) == 1
        imp.payee_mapper.update_last_used.assert_not_called()

    def test_no_mapping_dry_run_prints_fallback(self, monkeypatch, tmp_path, capsys):
        imp = self._setup(monkeypatch, tmp_path, dry_run=True)
        imp.payee_mapper.get_mapping.return_value = None
        imp.matcher.find_matching_accounts.return_value = []
        imp.matcher.suggest_category.return_value = "Expenses:Miscellaneous"
        imp.process_transactions([self._make_tx()])
        assert imp.tx_to_create == []
        assert "Expenses:Miscellaneous" in capsys.readouterr().out

    def test_exact_duplicate_dry_run_skips(self, monkeypatch, tmp_path):
        imp = self._setup(monkeypatch, tmp_path, dry_run=True, check_ledger=True)
        existing = self._make_txn("TESCO STORES", date(2026, 1, 15), [])
        monkeypatch.setattr(
            imp, "_exact_ledger_duplicate", lambda tx: DuplicateMatch(existing, 0.9)
        )
        imp.process_transactions([self._make_tx()])
        assert imp.tx_to_create == []

    def test_pending_duplicate_dry_run_skips(self, monkeypatch, tmp_path):
        imp = self._setup(monkeypatch, tmp_path, dry_run=True)
        monkeypatch.setattr(imp, "_pending_duplicate_decision", lambda tx: True)
        impl_tx = self._make_tx(is_pending=True)
        imp.process_transactions([impl_tx])
        assert imp.tx_to_create == []


class TestSourceAccountResolution(TestTransactionImporterHarness):
    def test_hint_resolves(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        account = self._make_account("Assets:Revolut GBP")
        imp.matcher = Mock()
        imp.matcher.get_account_guid.return_value = account.guid
        imp.matcher.get_account_by_guid.return_value = account
        assert imp.resolve_source_account("Assets:Revolut GBP") is account

    def test_no_postable_accounts_raises(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.matcher = Mock()
        imp.matcher.get_account_guid.return_value = None
        imp.matcher.accounts_cache = {}
        with pytest.raises(RuntimeError):
            imp.resolve_source_account(None)

    def test_interactive_index_selection(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        account = self._make_account("Assets:Revolut GBP")
        imp.matcher = Mock()
        imp.matcher.get_account_guid.return_value = None
        imp.matcher.accounts_cache = {account.guid: account}
        with patch.object(importer_module, "input_with_completion", return_value="1"):
            assert imp.resolve_source_account(None) is account


class TestAccountCreation(TestTransactionImporterHarness):
    def test_infer_account_type(self):
        assert TransactionImporter._infer_account_type("Income:Salary") == "INCOME"
        assert TransactionImporter._infer_account_type("Expenses:Food") == "EXPENSE"
        assert TransactionImporter._infer_account_type("Expense:Food") == "EXPENSE"
        assert TransactionImporter._infer_account_type("Assets:Bank") == "ASSET"
        assert TransactionImporter._infer_account_type("Liabilities:Card") == "LIABILITY"
        assert TransactionImporter._infer_account_type("Equity:Opening") == "EQUITY"
        assert TransactionImporter._infer_account_type("Foo:Bar") == "EXPENSE"

    def test_suggest_new_account_path_with_suggestions(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        suggestions = [(self._make_account("Expenses:Food"), 0.9, "x")]
        path = imp._suggest_new_account_path({"payee": "Tesco"}, suggestions)
        assert path == "Expenses:Food:Tesco"

    def test_suggest_new_account_path_fallback(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.matcher = Mock()
        imp.matcher.suggest_category.return_value = "Expenses:Miscellaneous"
        path = imp._suggest_new_account_path({"payee": "Tesco"}, [])
        assert path == "Expenses:Miscellaneous:Tesco"

    def test_create_account_path_too_short(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.matcher = Mock()
        imp.book = Mock()
        assert imp._create_account_path("Expenses", {"currency": "GBP"}) is None
        imp.book.add.assert_not_called()

    def test_create_account_path_creates_new_hierarchy(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.dry_run = True  # skips the on-disk backup
        imp.book = Mock()
        imp.book.commodities = []
        imp.book.root_account = Mock()
        imp.matcher = Mock()
        imp.matcher.get_account_guid.return_value = None
        imp.matcher.accounts_cache = {}
        account = Mock()
        account.guid = "new-guid"
        commodity = Mock()
        with patch.object(importer_module.piecash, "Account", return_value=account), \
             patch.object(importer_module.piecash, "Commodity", return_value=commodity):
            result = imp._create_account_path("Expenses:Food:Tesco", {"currency": "GBP"})
        assert result is account
        assert imp.book.add.call_count == 3
        imp.book.save.assert_called_once()
        assert imp.matcher.accounts_cache["new-guid"] is account

    def test_create_account_path_reuses_existing_parent(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.dry_run = True
        imp.book = Mock()
        imp.book.commodities = []
        imp.book.root_account = Mock()
        imp.matcher = Mock()
        existing = self._make_account("Expenses", "EXPENSE")
        imp.matcher.get_account_guid.side_effect = (
            lambda name: existing.guid if name == "Expenses" else None
        )
        imp.matcher.get_account_by_guid.return_value = existing
        imp.matcher.accounts_cache = {}
        with patch.object(
            importer_module.piecash, "Account", return_value=Mock(guid="new-guid")
        ), patch.object(importer_module.piecash, "Commodity", return_value=Mock()):
            result = imp._create_account_path("Expenses:Food", {"currency": "GBP"})
        assert result is not None
        assert imp.book.add.call_count == 1
        imp.book.save.assert_called_once()


class TestExecuteImport(TestTransactionImporterHarness):
    def _info(self, src=None, dest=None, hash_="h1", commodity=None):
        return {
            "source_account": src or self._make_account("Assets:Bank"),
            "dest_account": dest or self._make_account("Expenses:Food"),
            "date": "2026-01-15",
            "payee": "Tesco",
            "amount": Decimal("-25.50"),
            "commodity": commodity or Mock(mnemonic="GBP"),
            "memo": "",
            "hash": hash_,
        }

    def test_nothing_to_import(self, monkeypatch, tmp_path, capsys):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.tx_to_create = []
        imp.book = Mock()
        imp.matcher = Mock()
        imp.execute_import()
        assert "No transactions to import" in capsys.readouterr().out
        imp.book.save.assert_not_called()

    def test_creates_transaction_and_records(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.dry_run = True  # avoids touching the real book file for backups
        imp.book = Mock()
        imp.matcher = Mock()
        imp.matcher._acct_currency_matches.return_value = True
        info = self._info()
        imp.tx_to_create = [info]
        txn = Mock()
        txn.guid = "new-txn-guid"
        with patch.object(importer_module.piecash, "Transaction", return_value=txn), \
             patch.object(importer_module.piecash, "Split", return_value=Mock()):
            imp.execute_import()
        imp.book.add.assert_called_once_with(txn)
        imp.book.save.assert_called_once()
        record = imp.imported_tx["h1"]
        assert record["tx_guid"] == "new-txn-guid"
        assert record["payee"] == "Tesco"
        assert record["source_account"] == info["source_account"].fullname
        assert record["dest_account"] == info["dest_account"].fullname
        with open(os.path.join(str(tmp_path), ".imported_transactions.json")) as handle:
            saved = json.load(handle)
        assert saved["h1"]["tx_guid"] == "new-txn-guid"

    def test_currency_mismatch_skips_transaction(self, monkeypatch, tmp_path, capsys):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.dry_run = True
        imp.book = Mock()
        imp.matcher = Mock()
        imp.matcher._acct_currency_matches.return_value = False
        imp.tx_to_create = [self._info()]
        with patch.object(importer_module.piecash, "Transaction") as txn_cls:
            imp.execute_import()
        txn_cls.assert_not_called()
        imp.book.add.assert_not_called()
        assert imp.imported_tx == {}
        assert "1 skipped" in capsys.readouterr().out


class TestBackups(TestTransactionImporterHarness):
    def test_create_database_backup_copies_once(self, monkeypatch, tmp_path):
        book_path = os.path.join(str(tmp_path), "ledger.gnucash")
        with open(book_path, "w") as handle:
            handle.write("sqlite fake")
        imp = self._make_importer(monkeypatch, tmp_path, gnucash_file=book_path)
        imp.dry_run = False
        imp.keep_backups = 10
        imp._backup_created = False
        imp._create_database_backup()
        backups = [
            name for name in os.listdir(os.path.join(str(tmp_path), "backups"))
            if name.startswith("ledger.backup-") and name.endswith(".gnucash")
        ]
        assert len(backups) == 1
        assert imp._backup_created is True
        # The guard prevents creating a second backup for the same run.
        imp._create_database_backup()
        backups = os.listdir(os.path.join(str(tmp_path), "backups"))
        assert len([name for name in backups if name.endswith(".gnucash")]) == 1

    def test_prune_backups_keeps_newest(self, monkeypatch, tmp_path):
        imp = self._make_importer(monkeypatch, tmp_path)
        imp.keep_backups = 2
        directory = os.path.join(str(tmp_path), "backups")
        os.makedirs(directory)
        for index in range(4):
            path = os.path.join(directory, f"ledger.backup-2026010{index}.gnucash")
            with open(path, "w") as handle:
                handle.write(str(index))
            os.utime(path, (index + 1, index + 1))
        imp._prune_backups(directory, "ledger", ".gnucash")
        remaining = sorted(os.listdir(directory))
        assert remaining == [
            "ledger.backup-20260102.gnucash",
            "ledger.backup-20260103.gnucash",
        ]
