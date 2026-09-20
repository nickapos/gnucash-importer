"""Shared fixtures for the importer test suite."""
import os
import tempfile
import pytest


@pytest.fixture
def temp_dir():
    """Provide a temporary directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_revolut_csv(temp_dir):
    """Return path to a minimal Revolut CSV."""
    path = os.path.join(temp_dir, "revolut.csv")
    content = (
        "Type,Product,Started Date,Completed Date,Description,Amount,Currency,State,Balance\n"
        "CARDFEE,Revolut Metal,2026-01-01 00:00:00,2026-01-01 00:00:00,Monthly fee,-9.99,EUR,COMPLETED,100.00\n"
        "PAYMENT,Peer-to-Peer,2026-01-02 10:30:00,2026-01-02 10:30:00,Transfer to Friend,25.50,EUR,COMPLETED,125.50\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


@pytest.fixture
def sample_bof_scot_csv(temp_dir):
    """Return path to a minimal Bank of Scotland CSV."""
    path = os.path.join(temp_dir, "bof_scot.csv")
    content = (
        "Date,Description,Type,Money In (£),Money Out (£),Balance (£)\n"
        "02/01/2026,TESCO STORES,DEB,0.00,25.50,100.00\n"
        "03/01/2026,SALARY,PAY,1500.00,0.00,1600.00\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


@pytest.fixture
def sample_alpha_gr_csv(temp_dir):
    """Return path to a minimal Alpha Bank Greece CSV."""
    path = os.path.join(temp_dir, "alpha_gr.csv")
    content = (
        "Metadata Line 1\n"
        "Metadata Line 2\n"
        "Α/Α;Ημ/νία;Αιτιολογία;Κατάστημα;Τοκισμός από;Αρ. συναλλαγής;Ποσό;Πρόσημο ποσού\n"
        "1;02/01/2026;Cash Withdrawal;ATM;Bank;123456;100.00;Χ\n"
        "2;03/01/2026;Salary Credit;Employer;Bank;789012;2000.00;Π\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


@pytest.fixture
def sample_credit_card_csv(temp_dir):
    """Return path to a minimal credit-card CSV."""
    path = os.path.join(temp_dir, "credit_card.csv")
    content = (
        "Transaction Date,Transaction Cleared Date,Transaction Type,Transaction Description,Transaction Amount\n"
        "2026-01-15,,Purchase,Amazon Purchase,49.99\n"
        "2026-01-16,2026-01-16,Cleared Payment,Cash Advance,-200.00\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


@pytest.fixture
def sample_generic_csv(temp_dir):
    """Return path to a minimal generic CSV."""
    path = os.path.join(temp_dir, "generic.csv")
    content = (
        "date,payee,amount,currency,memo\n"
        "2026-01-15,Tesco Store,45.00,GBP,Groceries\n"
        "2026-01-16,Salary,2000.00,GBP,Monthly Pay\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path