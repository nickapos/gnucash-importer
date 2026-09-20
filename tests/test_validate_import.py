"""Tests for validate_import.py — import validation and date repair.

piecash is only installed in the project virtualenv, so this module is skipped
automatically under the system python (``python3``) via importorskip.

Run with:

    .venv/bin/python -m pytest tests/test_validate_import.py -q
"""
import json
import os
import sys
from datetime import date
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("piecash")

import validate_import
from utils import compute_tx_hash


class TestReadCsvRows:
    def test_generic_rows_get_import_hash(self, sample_generic_csv):
        rows = validate_import.read_csv_rows(sample_generic_csv)
        assert len(rows) == 2
        # The validator must reproduce exactly the importer's hash.
        for row in rows:
            assert row["import_hash"] == compute_tx_hash(row)

    def test_revolut_pending_rows_respected(self, temp_dir):
        path = os.path.join(temp_dir, "revolut.csv")
        content = (
            "Type,Product,Started Date,Completed Date,Description,Amount,Currency,State,Balance\n"
            "CARDFEE,Revolut Metal,2026-01-01 00:00:00,2026-01-01 00:00:00,Monthly fee,-9.99,EUR,PENDING,100.00\n"
            "PAYMENT,Peer-to-Peer,2026-01-02 00:00:00,2026-01-02 00:00:00,Transfer,25.50,EUR,COMPLETED,125.50\n"
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

        excluded = validate_import.read_csv_rows(path, skip_pending=True)
        assert len(excluded) == 1
        assert excluded[0]["payee"] == "Transfer"

        included = validate_import.read_csv_rows(path, skip_pending=False)
        assert len(included) == 2


class TestLoadImported:
    def test_missing_file_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            validate_import, "DUPLICATE_CHECK_FILE",
            os.path.join(str(tmp_path), "nope.json"),
        )
        assert validate_import.load_imported() == {}

    def test_valid_json_loaded(self, monkeypatch, tmp_path):
        path = os.path.join(str(tmp_path), "imported.json")
        with open(path, "w") as handle:
            json.dump({"h1": {"tx_guid": "g1"}}, handle)
        monkeypatch.setattr(validate_import, "DUPLICATE_CHECK_FILE", path)
        assert validate_import.load_imported() == {"h1": {"tx_guid": "g1"}}

    def test_corrupt_json_returns_empty_with_warning(self, monkeypatch, tmp_path, capsys):
        path = os.path.join(str(tmp_path), "imported.json")
        with open(path, "w") as handle:
            handle.write("{broken json")
        monkeypatch.setattr(validate_import, "DUPLICATE_CHECK_FILE", path)
        assert validate_import.load_imported() == {}
        assert "Warning" in capsys.readouterr().out


class TestMain:
    """End-to-end main() runs with a mocked piecash.open_book."""

    @staticmethod
    def _write_imported(tmp_path, data):
        path = os.path.join(str(tmp_path), ".imported_transactions.json")
        with open(path, "w") as handle:
            json.dump(data, handle)
        return path

    @staticmethod
    def _book_with(transactions):
        book = Mock()
        book.transactions = transactions
        return book

    @staticmethod
    def _txn(guid, post_date, description="Tesco Store"):
        txn = Mock()
        txn.guid = guid
        txn.post_date = post_date
        txn.description = description
        return txn

    def _run(self, monkeypatch, tmp_path, csv_path, imported, transactions,
             fix=False, gnucash_file="book.gnucash"):
        dup_path = self._write_imported(tmp_path, imported)
        monkeypatch.setattr(validate_import, "DUPLICATE_CHECK_FILE", dup_path)
        book = self._book_with(transactions)
        open_mock = patch.object(
            validate_import.piecash, "open_book", return_value=book
        )
        argv = [
            "validate_import.py",
            f"--gnucash-file={gnucash_file}",
            f"--csv-file={csv_path}",
        ]
        if fix:
            argv.append("--fix")
        monkeypatch.setattr(sys, "argv", argv)
        with open_mock as mock:
            validate_import.main()
        return book, mock

    @staticmethod
    def _record(import_hash, tx_guid, skipped=False):
        record = {
            "timestamp": "2026-01-01T00:00:00",
            "tx_guid": tx_guid,
            "payee": "Tesco Store",
            "amount": "45.00",
            "currency": "GBP",
            "csv_date": "2026-01-15",
        }
        if skipped:
            record["skipped"] = True
        return {import_hash: record}

    def test_all_rows_match_no_mismatch(self, monkeypatch, tmp_path, sample_generic_csv, capsys):
        rows = validate_import.read_csv_rows(sample_generic_csv)
        imported = {
            rows[0]["import_hash"]: {"tx_guid": "txn-1"},
            rows[1]["import_hash"]: {"tx_guid": "txn-2"},
        }
        transactions = [
            self._txn("txn-1", date(2026, 1, 15)),
            self._txn("txn-2", date(2026, 1, 16), description="Salary"),
        ]
        book, open_mock = self._run(
            monkeypatch, tmp_path, sample_generic_csv, imported, transactions
        )
        out = capsys.readouterr().out
        assert "Checked 2 previously-imported rows." in out
        assert "Found 0 date mismatch" in out
        assert "no matching import record" not in out
        # Without --fix the book must be opened readonly.
        assert open_mock.call_args.kwargs["readonly"] is True
        book.save.assert_not_called()

    def test_mismatch_reported_without_fix(self, monkeypatch, tmp_path, sample_generic_csv, capsys):
        rows = validate_import.read_csv_rows(sample_generic_csv)
        imported = {rows[0]["import_hash"]: {"tx_guid": "txn-1"}}
        transactions = [self._txn("txn-1", date(2026, 1, 10))]
        book, _ = self._run(
            monkeypatch, tmp_path, sample_generic_csv, imported, transactions
        )
        out = capsys.readouterr().out
        assert "MISMATCH" in out
        assert "book has 2026-01-10, CSV says 2026-01-15" in out
        assert "Run again with --fix" in out
        book.save.assert_not_called()

    def test_fix_corrects_dates_and_saves(self, monkeypatch, tmp_path, sample_generic_csv, capsys):
        rows = validate_import.read_csv_rows(sample_generic_csv)
        imported = {rows[0]["import_hash"]: {"tx_guid": "txn-1"}}
        txn = self._txn("txn-1", date(2026, 1, 10))
        book, open_mock = self._run(
            monkeypatch, tmp_path, sample_generic_csv, imported, [txn], fix=True
        )
        assert txn.post_date == date(2026, 1, 15)
        book.save.assert_called_once()
        assert open_mock.call_args.kwargs["readonly"] is False
        assert "Fixed 'Tesco Store': 2026-01-10 -> 2026-01-15" in capsys.readouterr().out

    def test_skipped_rows_reported(self, monkeypatch, tmp_path, sample_generic_csv, capsys):
        rows = validate_import.read_csv_rows(sample_generic_csv)
        imported = self._record(rows[0]["import_hash"], None, skipped=True)
        self._run(monkeypatch, tmp_path, sample_generic_csv, imported, [])
        out = capsys.readouterr().out
        assert "Checked 0 previously-imported rows." in out
        assert "1 row(s) were intentionally skipped-and-marked-imported." in out

    def test_legacy_rows_flagged(self, monkeypatch, tmp_path, sample_generic_csv, capsys):
        rows = validate_import.read_csv_rows(sample_generic_csv)
        imported = {
            rows[0]["import_hash"]: {
                "timestamp": "2026-01-01T00:00:00",
                "payee": "Tesco Store",
                "amount": "45.00",
                "currency": "GBP",
                "csv_date": "2026-01-15",
            }
        }
        self._run(monkeypatch, tmp_path, sample_generic_csv, imported, [])
        out = capsys.readouterr().out
        assert "1 row(s) are legacy imports without tx_guid (unverifiable)." in out
        assert "cannot verify automatically" in out

    def test_recorded_guid_missing_from_book(self, monkeypatch, tmp_path, sample_generic_csv, capsys):
        rows = validate_import.read_csv_rows(sample_generic_csv)
        imported = {rows[0]["import_hash"]: {"tx_guid": "gone"}}
        self._run(monkeypatch, tmp_path, sample_generic_csv, imported, [])
        out = capsys.readouterr().out
        assert "recorded tx_guid gone not found in book" in out
        assert "may have been deleted" in out

    def test_rows_without_import_record_reported(self, monkeypatch, tmp_path, sample_generic_csv, capsys):
        self._run(monkeypatch, tmp_path, sample_generic_csv, {}, [])
        out = capsys.readouterr().out
        assert "2 CSV row(s) have no matching import record" in out