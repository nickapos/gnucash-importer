"""Tests for mappings.py — persistent payee mappings and history analysis."""
import json
import os
from datetime import datetime
from unittest.mock import Mock
from mappings import PayeeAccountMapper, TransactionHistoryAnalyzer


class TestPayeeAccountMapper:
    """Unit tests for PayeeAccountMapper."""

    def test_initial_empty_mappings(self, temp_dir):
        mapper = PayeeAccountMapper(os.path.join(temp_dir, "mappings.json"))
        assert mapper.mappings == {}
        assert mapper.get_mapping("Tesco") is None

    def test_add_mapping_and_save(self, temp_dir):
        path = os.path.join(temp_dir, "mappings.json")
        mapper = PayeeAccountMapper(path)
        mapper.add_mapping("Tesco", "Expenses:Food", "guid-food")
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["Tesco"]["account_fullname"] == "Expenses:Food"
        assert data["Tesco"]["account_guid"] == "guid-food"
        assert data["Tesco"]["use_count"] == 1

    def test_add_mapping_updates_use_count(self, temp_dir):
        path = os.path.join(temp_dir, "mappings.json")
        mapper = PayeeAccountMapper(path)
        mapper.add_mapping("Tesco", "Expenses:Food", "guid-food")
        mapper.add_mapping("Tesco", "Expenses:Food", "guid-food")
        assert mapper.mappings["Tesco"]["use_count"] == 2

    def test_update_last_used(self, temp_dir):
        path = os.path.join(temp_dir, "mappings.json")
        mapper = PayeeAccountMapper(path)
        mapper.add_mapping("Tesco", "Expenses:Food", "guid-food")
        mapper.update_last_used("Tesco")
        assert "last_used" in mapper.mappings["Tesco"]
        assert mapper.mappings["Tesco"]["use_count"] == 2

    def test_update_last_used_unknown_payee(self, temp_dir):
        mapper = PayeeAccountMapper(os.path.join(temp_dir, "mappings.json"))
        mapper.update_last_used("Unknown")
        assert "Unknown" not in mapper.mappings

    def test_remove_mapping(self, temp_dir):
        path = os.path.join(temp_dir, "mappings.json")
        mapper = PayeeAccountMapper(path)
        mapper.add_mapping("Tesco", "Expenses:Food", "guid-food")
        assert mapper.remove_mapping("Tesco") is True
        assert mapper.remove_mapping("Tesco") is False
        assert "Tesco" not in mapper.mappings

    def test_list_mappings_sorted_by_use_count(self, temp_dir):
        path = os.path.join(temp_dir, "mappings.json")
        mapper = PayeeAccountMapper(path)
        mapper.add_mapping("Tesco", "Expenses:Food", "guid-food")
        mapper.update_last_used("Tesco")
        mapper.add_mapping("Amazon", "Expenses:Shopping", "guid-shopping")
        items = mapper.list_mappings()
        assert items[0][0] == "Tesco"

    def test_get_mapping_exact_match(self, temp_dir):
        mapper = PayeeAccountMapper(os.path.join(temp_dir, "mappings.json"))
        mapper.add_mapping("Tesco", "Expenses:Food", "guid-food")
        assert mapper.get_mapping("Tesco") is not None

    def test_get_mapping_case_insensitive(self, temp_dir):
        mapper = PayeeAccountMapper(os.path.join(temp_dir, "mappings.json"))
        mapper.add_mapping("Tesco", "Expenses:Food", "guid-food")
        assert mapper.get_mapping("tesco") is not None

    def test_get_mapping_partial_match(self, temp_dir):
        mapper = PayeeAccountMapper(os.path.join(temp_dir, "mappings.json"))
        mapper.add_mapping("Tesco Supermarket", "Expenses:Food", "guid-food")
        assert mapper.get_mapping("Tesco") is not None

    def test_load_invalid_json_returns_empty(self, temp_dir):
        path = os.path.join(temp_dir, "mappings.json")
        with open(path, "w") as f:
            f.write("{not valid json")
        mapper = PayeeAccountMapper(path)
        assert mapper.mappings == {}

    def test_load_nonexistent_file_returns_empty(self, temp_dir):
        mapper = PayeeAccountMapper(os.path.join(temp_dir, "nonexistent.json"))
        assert mapper.mappings == {}


class TestTransactionHistoryAnalyzer:
    """Unit tests for TransactionHistoryAnalyzer."""

    def test_extract_words_basic(self):
        analyzer = TransactionHistoryAnalyzer(Mock())
        words = analyzer.extract_words("Tesco Superstore Purchase")
        assert "tesco" in words
        assert "superstore" in words
        assert "purchase" in words

    def test_extract_words_stops(self):
        analyzer = TransactionHistoryAnalyzer(Mock())
        words = analyzer.extract_words("The and or but in on at to for of with by")
        assert words == []

    def test_extract_words_single_letter_removed(self):
        analyzer = TransactionHistoryAnalyzer(Mock())
        words = analyzer.extract_words("A a b c shop")
        assert words == ["shop"]

    def test_analyze_populates_payee_to_accounts(self, temp_dir):
        analyzer = TransactionHistoryAnalyzer(Mock(), os.path.join(temp_dir, "history.json"))
        # Mock book with transactions
        book = Mock()
        txn = Mock()
        txn.post_date = datetime(2026, 1, 15)
        txn.description = "Tesco Superstore"
        split = Mock()
        split.account.fullname = "Expenses:Food"
        txn.splits = [split]
        book.transactions = [txn]
        analyzer.book = book
        analyzer.analyze(max_transactions=10)
        assert "Tesco Superstore" in analyzer.payee_to_accounts
        assert analyzer.payee_to_accounts["Tesco Superstore"][0]["account"] == "Expenses:Food"
        # Sanity check: analysis metadata (incl. last_analyzed) is persisted
        # to the cache file, not stored on the in-memory word_patterns dict.
        with open(os.path.join(temp_dir, "history.json")) as handle:
            cache = json.load(handle)
        assert cache["last_analyzed"]
        assert cache["transactions_analyzed"] == 1

    def test_analyze_sorts_mixed_post_dates(self, temp_dir):
        """analyze() must not crash when some txns store date vs datetime."""
        analyzer = TransactionHistoryAnalyzer(Mock(), os.path.join(temp_dir, "history.json"))
        book = Mock()

        txn1 = Mock()
        txn1.post_date = datetime(2026, 1, 15)  # datetime
        txn1.enter_date = None
        txn1.description = "Alpha"
        split1 = Mock()
        split1.account.fullname = "Expenses:Food"
        txn1.splits = [split1]

        txn2 = Mock()
        txn2.post_date = None
        txn2.enter_date = datetime(2026, 1, 1, 9, 0)  # datetime fallback
        txn2.description = "Beta"
        split2 = Mock()
        split2.account.fullname = "Expenses:Travel"
        txn2.splits = [split2]

        txn3 = Mock()
        txn3.post_date = datetime(2026, 1, 1).date()  # plain date
        txn3.enter_date = None
        txn3.description = "Gamma"
        split3 = Mock()
        split3.account.fullname = "Expenses:Travel"
        txn3.splits = [split3]

        book.transactions = [txn1, txn2, txn3]
        analyzer.book = book
        # Mixed date/datetime + None post_date must not raise inside list.sort.
        analyzer.analyze(max_transactions=10)
        assert "Alpha" in analyzer.payee_to_accounts
        assert "Beta" in analyzer.payee_to_accounts
        assert "Gamma" in analyzer.payee_to_accounts

    def test_analyze_resets_word_patterns(self, temp_dir):
        """Repeated analyze() calls must not double-count history."""
        analyzer = TransactionHistoryAnalyzer(Mock(), os.path.join(temp_dir, "history.json"))
        book = Mock()
        txn = Mock()
        txn.post_date = datetime(2026, 1, 15)
        txn.enter_date = None
        txn.description = "Tesco Superstore"
        split = Mock()
        split.account.fullname = "Expenses:Food"
        txn.splits = [split]
        book.transactions = [txn]
        analyzer.book = book

        analyzer.analyze(max_transactions=10)
        assert analyzer.word_patterns["tesco"]["Expenses:Food"] == 1
        # A second analysis of the same book must recount from scratch.
        analyzer.analyze(max_transactions=10)
        assert analyzer.word_patterns["tesco"]["Expenses:Food"] == 1

    def test_suggest_mapping_exact_match(self):
        analyzer = TransactionHistoryAnalyzer(Mock())
        analyzer.payee_to_accounts = {
            "Tesco Superstore": [{"account": "Expenses:Food", "count": 5, "confidence": 1.0}]
        }
        suggestions = analyzer.suggest_mapping("Tesco Superstore")
        assert len(suggestions) >= 1
        assert suggestions[0][0] == "Expenses:Food"

    def test_suggest_mapping_fuzzy_match(self):
        analyzer = TransactionHistoryAnalyzer(Mock())
        analyzer.payee_to_accounts = {
            "Tesco Superstore": [{"account": "Expenses:Food", "count": 5, "confidence": 1.0}]
        }
        suggestions = analyzer.suggest_mapping("Tesco Supermarket")
        assert len(suggestions) >= 1
        assert suggestions[0][0] == "Expenses:Food"

    def test_suggest_mapping_word_pattern(self):
        analyzer = TransactionHistoryAnalyzer(Mock())
        analyzer.word_patterns = {
            "tesco": {"Expenses:Food": 5, "Expenses:Miscellaneous": 1}
        }
        suggestions = analyzer.suggest_mapping("Tesco", "Superstore")
        assert len(suggestions) >= 1
        assert "Expenses:Food" in [item[0] for item in suggestions]