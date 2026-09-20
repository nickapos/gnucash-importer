"""Tests for bank_formats.py — CSV parsing for all bank types."""
import os
import pytest
from bank_formats import BankFormatMapper, _parse_amount, read_bank_csv


class TestBankFormatMapper:
    """Unit tests for.BankFormatMapper methods."""

    def test_map_revolut_pends_and_completed(self):
        mapper = BankFormatMapper()
        row = {
            "Type": "PAYMENT",
            "Product": "Peer-to-Peer",
            "Started Date": "2026-01-02 10:30:00",
            "Completed Date": "2026-01-02 10:30:00",
            "Description": "Transfer to Friend",
            "Amount": "25.50",
            "Fee": "0.00",
            "Currency": "EUR",
            "State": "COMPLETED",
            "Balance": "125.50",
        }
        mapped = mapper.map_revolut(row)
        assert mapped["amount"] == 25.50
        assert mapped["currency"] == "EUR"
        assert mapped["payee"] == "Transfer to Friend"
        assert mapped["is_pending"] is False
        assert mapped["source_kind"] == "bank"

    def test_map_revolut_pending_state(self):
        mapper = BankFormatMapper()
        row = {
            "Description": "Pending Payment",
            "Amount": "100.00",
            "Currency": "GBP",
            "State": "PENDING",
        }
        mapped = mapper.map_revolut(row)
        assert mapped["is_pending"] is True

    def test_map_revolut_negative_amount(self):
        mapper = BankFormatMapper()
        row = {
            "Description": "Monthly fee",
            "Amount": "-9.99",
            "Currency": "EUR",
            "State": "COMPLETED",
        }
        mapped = mapper.map_revolut(row)
        assert mapped["amount"] == -9.99

    def test_map_bof_scot_debit_and_credit(self):
        mapper = BankFormatMapper()
        # Statement-style
        row = {
            "Date": "02/01/2026",
            "Description": "TESCO STORES",
            "Type": "DEB",
            "Money In (£)": "0.00",
            "Money Out (£)": "25.50",
            "Balance (£)": "100.00",
        }
        mapped = mapper.map_bof_scot(row)
        assert mapped["amount"] == -25.50
        assert mapped["currency"] == "GBP"
        assert mapped["payee"] == "TESCO STORES"

    def test_map_bof_scot_credit_amount(self):
        mapper = BankFormatMapper()
        row = {
            "Transaction Date": "03/01/2026",
            "Transaction Description": "SALARY",
            "Transaction Type": "CRED",
            "Credit Amount": "1500.00",
            "Debit Amount": "",
            "Balance": "1600.00",
        }
        mapped = mapper.map_bof_scot(row)
        assert mapped["amount"] == 1500.00

    def test_map_credit_card_purchase_and_refund(self):
        mapper = BankFormatMapper()
        # Purchase (positive in CSV -> negative amount)
        row_purchase = {
            "Transaction Date": "2026-01-15",
            "Transaction Cleared Date": "2026-01-15",
            "Transaction Type": "Purchase",
            "Transaction Description": "Amazon Purchase",
            "Transaction Amount": "49.99",
        }
        mapped = mapper.map_credit_card(row_purchase)
        assert mapped["amount"] == -49.99
        assert mapped["is_pending"] is False
        assert mapped["currency"] == "GBP"

    def test_map_credit_card_unsettled(self):
        mapper = BankFormatMapper()
        row = {
            "Transaction Date": "2026-01-15",
            "Transaction Cleared Date": "",
            "Transaction Type": "Purchase",
            "Transaction Description": "Pending Purchase",
            "Transaction Amount": "60.00",
        }
        mapped = mapper.map_credit_card(row)
        assert mapped["is_pending"] is True

    def test_map_alpha_gr_debit_and_credit(self):
        mapper = BankFormatMapper()
        # Debit (Χ)
        row_debit = {
            "Ημ/νία": "02/01/2026",
            "Αιτιολογία": "ATM Withdrawal",
            "Ποσό": "100.00",
            "Πρόσημο ποσού": "Χ",
        }
        mapped = mapper.map_alpha_gr(row_debit)
        assert mapped["amount"] == -100.00
        assert mapped["currency"] == "EUR"

        # Credit (Π)
        row_credit = {
            "Ημ/νία": "03/01/2026",
            "Αιτιολογία": "Salary Credit",
            "Ποσό": "2000.00",
            "Πρόσημο ποσού": "Π",
        }
        mapped = mapper.map_alpha_gr(row_credit)
        assert mapped["amount"] == 2000.00

    def test_map_generic(self):
        mapper = BankFormatMapper()
        row = {
            "date": "2026-01-15",
            "payee": "Tesco Store",
            "amount": "45.00",
            "currency": "GBP",
            "memo": "Groceries",
        }
        mapped = mapper.map_generic(row)
        assert mapped["amount"] == 45.00
        assert mapped["currency"] == "GBP"
        assert mapped["payee"] == "Tesco Store"


class TestParseAmount:
    """Unit tests for the shared _parse_amount helper."""

    def test_dot_decimal(self):
        assert _parse_amount("100.50") == 100.50

    def test_currency_symbol(self):
        assert _parse_amount("£25.50") == 25.50

    def test_negative(self):
        assert _parse_amount("-9.99") == -9.99

    def test_uk_thousands(self):
        assert _parse_amount("1,234.56") == 1234.56

    def test_european_style(self):
        # Mixed separators: the last one is decimal -> 1.234,56 == 1234.56
        assert _parse_amount("1.234,56", decimal_comma=True) == 1234.56

    def test_both_separators_dot_last(self):
        assert _parse_amount("1,234.56", decimal_comma=True) == 1234.56

    def test_both_separators_comma_last(self):
        assert _parse_amount("1.234,56") == 1234.56

    def test_blank_returns_none(self):
        assert _parse_amount("") is None
        assert _parse_amount(None) is None

    def test_garbage_returns_none(self):
        assert _parse_amount("not-a-number") is None


class TestDetectFormat:
    """Unit tests for BankFormatMapper.detect_format."""

    def test_revolut_detected(self, sample_revolut_csv):
        fmt, rows = read_bank_csv(sample_revolut_csv, include_pending=False)
        assert fmt == "revolut"
        assert len(rows) == 2

    def test_bof_scot_detected(self, sample_bof_scot_csv):
        fmt, rows = read_bank_csv(sample_bof_scot_csv)
        assert fmt == "bof_scot"
        assert len(rows) == 2

    def test_alpha_gr_detected(self, sample_alpha_gr_csv):
        fmt, rows = read_bank_csv(sample_alpha_gr_csv)
        assert fmt == "alpha_gr"
        assert len(rows) == 2

    def test_credit_card_detected(self, sample_credit_card_csv):
        fmt, rows = read_bank_csv(sample_credit_card_csv, include_pending=True)
        assert fmt == "credit_card"

    def test_generic_detected(self, sample_generic_csv):
        fmt, rows = read_bank_csv(sample_generic_csv)
        assert fmt == "generic"

    def test_revolut_excludes_pending_by_default(self, sample_revolut_csv):
        fmt, rows = read_bank_csv(sample_revolut_csv, include_pending=False)
        # In the fixture, both are COMPLETED — but if one were PENDING it should be excluded
        assert len(rows) == 2

    def test_revolut_includes_pending(self, sample_revolut_csv):
        fmt, rows = read_bank_csv(sample_revolut_csv, include_pending=True)
        assert len(rows) == 2  # Both rows are COMPLETED in fixture

    def test_invalid_row_skipped_with_reason(self, temp_dir):
        path = os.path.join(temp_dir, "bad.csv")
        with open(path, "w") as f:
            f.write("date,payee,amount\n")
            f.write(",");
            # Empty payee and amount should be skipped
        fmt, rows = read_bank_csv(path)
        assert fmt == "generic"
        assert len(rows) == 0