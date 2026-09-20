"""Tests for utils.py — date parsing, normalization, hashing, helpers."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import (  # noqa: E402
    AccountPathCompleter,
    atomic_write_json,
    compute_tx_hash,
    normalize_payee,
    parse_tx_date,
    tokenize,
)


class TestParseTxDate(unittest.TestCase):
    def test_iso_date_only(self):
        self.assertEqual(parse_tx_date("2026-01-15"), datetime(2026, 1, 15))

    def test_iso_datetime(self):
        self.assertEqual(parse_tx_date("2026-01-15 10:30:00"), datetime(2026, 1, 15, 10, 30, 0))

    def test_iso_with_timezone(self):
        result = parse_tx_date("2026-01-15T10:30:00+00:00")
        self.assertEqual((result.year, result.month, result.day), (2026, 1, 15))

    def test_european_dmy_slash(self):
        self.assertEqual(parse_tx_date("15/01/2026"), datetime(2026, 1, 15))

    def test_european_dmy_slash_datetime(self):
        self.assertEqual(parse_tx_date("15/01/2026 10:30:00"), datetime(2026, 1, 15, 10, 30, 0))

    def test_european_dmy_dash(self):
        self.assertEqual(parse_tx_date("15-01-2026"), datetime(2026, 1, 15))

    def test_uk_style_short_year(self):
        self.assertEqual(parse_tx_date("15 Jan 26"), datetime(2026, 1, 15))

    def test_uk_style_full_year(self):
        self.assertEqual(parse_tx_date("15 January 2026"), datetime(2026, 1, 15))

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse_tx_date("")

    def test_none_raises(self):
        with self.assertRaises(ValueError):
            parse_tx_date(None)

    def test_whitespace_raises(self):
        with self.assertRaises(ValueError):
            parse_tx_date("   ")

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            parse_tx_date("not-a-date")


class TestNormalizePayee(unittest.TestCase):
    def test_lowercasing(self):
        self.assertEqual(normalize_payee("TESCO STORE"), "tesco store")

    def test_special_chars_become_spaces(self):
        self.assertEqual(normalize_payee("Tesco Store #123!"), "tesco store 123")

    def test_multiple_spaces_collapsed(self):
        self.assertEqual(normalize_payee("Tesco   Store   #123"), "tesco store 123")

    def test_unicode_preserved(self):
        self.assertEqual(normalize_payee("ΑΙΤΙΟΛΟΓΙΑ"), "αιτιολογια")

    def test_empty(self):
        self.assertEqual(normalize_payee(""), "")

    def test_none(self):
        self.assertEqual(normalize_payee(None), "")


class TestTokenize(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(tokenize("Tesco Superstore Purchase"), {"tesco", "superstore", "purchase"})

    def test_stop_words_removed(self):
        self.assertEqual(tokenize("the and or but in on at to for of with by"), set())

    def test_short_words_removed(self):
        self.assertEqual(tokenize("a b c shop"), {"shop"})

    def test_unicode_preserved(self):
        # Non-Latin payees (e.g. Greek Alpha Bank rows) must tokenize like
        # any other word instead of being stripped to an empty set.
        self.assertEqual(tokenize("ΑΝΑΛΗΨΗ ΜΕΤΡΗΤΩΝ"), {"αναληψη", "μετρητων"})

    def test_empty(self):
        self.assertEqual(tokenize(""), set())

    def test_none(self):
        self.assertEqual(tokenize(None), set())


class TestComputeTxHash(unittest.TestCase):
    def test_stable(self):
        tx = {"date": "2026-01-15", "payee": "Tesco", "amount": 25.5}
        first = compute_tx_hash(tx)
        self.assertEqual(first, compute_tx_hash(dict(tx)))
        self.assertEqual(len(first), 32)

    def test_amount_normalized_to_two_decimals(self):
        a = compute_tx_hash({"date": "2026-01-15", "payee": "Tesco", "amount": 25.5})
        b = compute_tx_hash({"date": "2026-01-15", "payee": "Tesco", "amount": 25.50})
        self.assertEqual(a, b)

    def test_different_payee_differs(self):
        a = compute_tx_hash({"date": "2026-01-15", "payee": "Tesco", "amount": 25.5})
        b = compute_tx_hash({"date": "2026-01-15", "payee": "Sainsbury", "amount": 25.5})
        self.assertNotEqual(a, b)


class TestAtomicWriteJson(unittest.TestCase):
    def test_writes_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.json")
            atomic_write_json(path, {"a": 1, "b": [2, 3]})
            with open(path) as handle:
                self.assertEqual(json.load(handle), {"a": 1, "b": [2, 3]})

    def test_no_temp_files_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.json")
            atomic_write_json(path, {"a": 1})
            leftovers = [name for name in os.listdir(tmp) if name.startswith(".tmp-")]
            self.assertEqual(leftovers, [])


class TestAccountPathCompleter(unittest.TestCase):
    def test_prefix_match(self):
        completer = AccountPathCompleter(["Assets:Bank", "Assets:Savings", "Expenses:Food"])
        self.assertEqual(completer.complete("Assets:", 0), "Assets:Bank")
        self.assertEqual(completer.complete("Assets:", 1), "Assets:Savings")

    def test_no_match(self):
        completer = AccountPathCompleter(["Assets:Bank"])
        self.assertIsNone(completer.complete("Expenses:", 0))

    def test_case_insensitive(self):
        completer = AccountPathCompleter(["Assets:Bank"])
        self.assertEqual(completer.complete("assets:b", 0), "Assets:Bank")

    def test_all_options_and_out_of_range(self):
        completer = AccountPathCompleter(["A", "B", "C"])
        self.assertEqual(completer.complete("", 0), "A")
        self.assertEqual(completer.complete("", 2), "C")
        self.assertIsNone(completer.complete("", 3))

    def test_duplicates_removed_and_sorted(self):
        completer = AccountPathCompleter(["Zulu", "Alpha", "Alpha", "Mike"])
        self.assertEqual(completer.complete("", 0), "Alpha")
        self.assertEqual(completer.complete("", 1), "Mike")
        self.assertEqual(completer.complete("", 2), "Zulu")
        self.assertIsNone(completer.complete("", 3))


if __name__ == "__main__":
    unittest.main()
