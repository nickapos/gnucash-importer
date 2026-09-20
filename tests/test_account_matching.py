"""Tests for account_matching.py — account cache and suggestions."""
from unittest.mock import Mock
from account_matching import AccountMatcher
from mappings import PayeeAccountMapper


class TestAccountMatcher:
    """Unit tests for AccountMatcher."""

    @staticmethod
    def _make_matcher(payee_mapper=None):
        """Return an AccountMatcher with a mock book (empty account list).

        Individual tests override ``matcher.accounts_cache`` afterwards, so the
        book-backed cache only needs to be an iterable here.
        """
        book = Mock()
        book.accounts = []
        history_analyzer = Mock()
        history_analyzer.suggest_mapping.return_value = []
        return AccountMatcher(book, payee_mapper if payee_mapper is not None else Mock(), history_analyzer)

    @staticmethod
    def _make_account(name, fullname, account_type="EXPENSE", commodity_mnemonic="GBP"):
        account = Mock()
        account.name = name
        account.fullname = fullname
        account.guid = f"guid-{name}"
        account.type = account_type
        account.placeholder = 0
        commodity = Mock()
        commodity.namespace = "CURRENCY"
        commodity.mnemonic = commodity_mnemonic
        account.commodity = commodity
        return account

    def test_get_account_by_guid_found(self):
        matcher = self._make_matcher()
        account = self._make_account("Food", "Expenses:Food")
        matcher.accounts_cache = {account.guid: account}
        result = matcher.get_account_by_guid(account.guid)
        assert result == account

    def test_get_account_by_guid_not_found(self):
        matcher = self._make_matcher()
        matcher.accounts_cache = {}
        assert matcher.get_account_by_guid("nonexistent") is None

    def test_get_account_guid_by_fullname(self):
        matcher = self._make_matcher()
        account = self._make_account("Food", "Expenses:Food")
        matcher.accounts_cache = {account.guid: account}
        assert matcher.get_account_guid("Expenses:Food") == account.guid

    def test_get_account_guid_by_name(self):
        matcher = self._make_matcher()
        account = self._make_account("Food", "Expenses:Food")
        matcher.accounts_cache = {account.guid: account}
        assert matcher.get_account_guid("Food") == account.guid

    def test_currency_matches_true(self):
        matcher = self._make_matcher()
        account = self._make_account("Food", "Expenses:Food", commodity_mnemonic="GBP")
        assert matcher._acct_currency_matches(account, "GBP") is True

    def test_currency_matches_case_insensitive(self):
        matcher = self._make_matcher()
        account = self._make_account("Food", "Expenses:Food", commodity_mnemonic="gbp")
        assert matcher._acct_currency_matches(account, "GBP") is True

    def test_currency_matches_false(self):
        matcher = self._make_matcher()
        account = self._make_account("Food", "Expenses:Food", commodity_mnemonic="USD")
        assert matcher._acct_currency_matches(account, "GBP") is False

    def test_currency_matches_none_mnemonic(self):
        matcher = self._make_matcher()
        account = Mock()
        account.commodity = Mock()
        account.commodity.namespace = "CURRENCY"
        account.commodity.mnemonic = None
        assert matcher._acct_currency_matches(account, "GBP") is False

    def test_currency_matches_no_commodity(self):
        matcher = self._make_matcher()
        account = Mock()
        account.commodity = None
        assert matcher._acct_currency_matches(account, "GBP") is False

    def test_currency_matches_no_currency_arg(self):
        matcher = self._make_matcher()
        account = self._make_account("Food", "Expenses:Food", commodity_mnemonic="GBP")
        assert matcher._acct_currency_matches(account, "") is False

    def test_suggest_category_food(self):
        matcher = self._make_matcher()
        assert matcher.suggest_category("Tesco Superstore") == "Expenses:Food"
        assert matcher.suggest_category("Sainsburys") == "Expenses:Food"

    def test_suggest_category_shopping(self):
        matcher = self._make_matcher()
        assert matcher.suggest_category("Amazon Purchase") == "Expenses:Shopping"

    def test_suggest_category_transportation(self):
        matcher = self._make_matcher()
        assert matcher.suggest_category("Shell Petrol Station") == "Expenses:Transportation"
        assert matcher.suggest_category("Uber Ride") == "Expenses:Transportation"

    def test_suggest_category_subscriptions(self):
        matcher = self._make_matcher()
        assert matcher.suggest_category("Netflix Monthly") == "Expenses:Subscriptions"

    def test_suggest_category_salary(self):
        matcher = self._make_matcher()
        assert matcher.suggest_category("Monthly Salary Payment") == "Income:Salary"

    def test_suggest_category_default(self):
        matcher = self._make_matcher()
        assert matcher.suggest_category("Unknown Mystery Payee") == "Expenses:Miscellaneous"

    def test_find_matching_accounts_exact_name(self):
        mapper = PayeeAccountMapper()
        matcher = self._make_matcher(mapper)
        account = self._make_account("Tesco", "Expenses:Food")
        matcher.accounts_cache = {account.guid: account}
        suggestions = matcher.find_matching_accounts("Tesco", "Food", "GBP")
        assert len(suggestions) >= 1
        best_account, confidence, reason = suggestions[0]
        assert best_account.fullname == "Expenses:Food"

    def test_find_matching_accounts_token_overlap(self):
        mapper = PayeeAccountMapper()
        matcher = self._make_matcher(mapper)
        account = self._make_account("Travel", "Expenses:Travel")
        matcher.accounts_cache = {account.guid: account}
        suggestions = matcher.find_matching_accounts("Uber Travel", "Transport", "GBP")
        assert len(suggestions) >= 1

    def test_find_matching_accounts_excludes_source_guid(self):
        mapper = PayeeAccountMapper()
        matcher = self._make_matcher(mapper)
        source_account = self._make_account("Source", "Assets:Bank")
        dest_account = self._make_account("Food", "Expenses:Food")
        matcher.accounts_cache = {
            source_account.guid: source_account,
            dest_account.guid: dest_account,
        }
        suggestions = matcher.find_matching_accounts("Food", "", "GBP", exclude_guid=source_account.guid)
        for suggestion in suggestions:
            assert suggestion[0].guid != source_account.guid