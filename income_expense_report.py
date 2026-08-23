#!/usr/bin/env python3
"""
income_expense_report.py
=========================
Create a monthly income, expense, and cash-flow report from a SQLite-backed
GnuCash book.

The database is opened read-only using sqlite3. This script does not use
piecash, does not modify the book, and does not create a GnuCash lock.

Examples
--------

# Print separate reports for every currency found in 2026
python income_expense_report.py \
  --gnucash-file portfolio-sqlite.gnucash \
  --year 2026

# GBP report with annual account/category detail and CSV output
python income_expense_report.py \
  --gnucash-file portfolio-sqlite.gnucash \
  --year 2026 \
  --currency GBP \
  --detail \
  --csv-output income-expense-2026-gbp.csv

# EUR report and a cash-flow PNG chart
python income_expense_report.py \
  --gnucash-file portfolio-sqlite.gnucash \
  --year 2026 \
  --currency EUR \
  --plot cashflow-2026-eur.png

Plotting requires matplotlib:
    pip install matplotlib
"""

import argparse
import calendar
import csv
import os
import sqlite3
from collections import defaultdict
from decimal import Decimal
from typing import Dict, Iterable, Optional, Tuple


MONTHS = list(range(1, 13))


def decimal_from_fraction(numerator, denominator) -> Decimal:
    """Convert GnuCash integer numerator/denominator storage to Decimal."""
    if denominator in (None, 0):
        return Decimal("0")
    return Decimal(int(numerator)) / Decimal(int(denominator))


def open_readonly_database(path: str) -> sqlite3.Connection:
    absolute_path = os.path.abspath(path)
    if not os.path.exists(absolute_path):
        raise FileNotFoundError(f"GnuCash file not found: {absolute_path}")
    conn = sqlite3.connect(f"file:{absolute_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_account_fullnames(conn: sqlite3.Connection) -> Dict[str, str]:
    """Reconstruct Assets:Parent:Child names from accounts.parent_guid."""
    rows = conn.execute("SELECT guid, name, parent_guid FROM accounts").fetchall()
    accounts = {
        row["guid"]: {"name": row["name"] or "", "parent_guid": row["parent_guid"]}
        for row in rows
    }
    cache: Dict[str, str] = {}

    def fullname(guid: str, trail=None) -> str:
        if guid in cache:
            return cache[guid]
        if trail is None:
            trail = set()
        if guid in trail:
            return accounts.get(guid, {}).get("name", guid)
        trail.add(guid)
        account = accounts.get(guid)
        if account is None:
            return ""
        parent_guid = account["parent_guid"]
        parent_name = fullname(parent_guid, trail) if parent_guid else ""
        result = f"{parent_name}:{account['name']}" if parent_name else account["name"]
        cache[guid] = result
        return result

    for guid in accounts:
        fullname(guid)
    return cache


def fetch_report_splits(
    conn: sqlite3.Connection, year: int, currency: Optional[str]
) -> Iterable[sqlite3.Row]:
    """Fetch income and expense splits for a year.

    quantity_num / quantity_denom are deliberately used instead of
    value_num / value_denom. Quantity is denominated in the account's own
    commodity, so this produces a correct report for the selected account
    currency even when a transaction involves multiple commodities.
    """
    query = """
        SELECT
            t.post_date AS post_date,
            t.description AS description,
            a.guid AS account_guid,
            a.account_type AS account_type,
            c.mnemonic AS currency,
            s.quantity_num AS quantity_num,
            s.quantity_denom AS quantity_denom
        FROM splits AS s
        INNER JOIN transactions AS t ON t.guid = s.tx_guid
        INNER JOIN accounts AS a ON a.guid = s.account_guid
        LEFT JOIN commodities AS c ON c.guid = a.commodity_guid
        WHERE substr(t.post_date, 1, 4) = ?
          AND a.account_type IN ('INCOME', 'EXPENSE')
    """
    params = [str(year)]
    if currency:
        query += " AND c.mnemonic = ?"
        params.append(currency.upper())
    query += " ORDER BY t.post_date, a.account_type, a.name"
    return conn.execute(query, params)


def make_report(
    conn: sqlite3.Connection, year: int, currency: Optional[str]
) -> Tuple[dict, dict]:
    """Return monthly totals and account-level category breakdowns.

    GnuCash normally represents income splits as credits (negative quantity)
    and expense splits as debits (positive quantity). The report inverts
    income for human-readable positive income totals while retaining signed
    behaviour for refunds/reversals.
    """
    fullnames = build_account_fullnames(conn)
    monthly = defaultdict(
        lambda: defaultdict(lambda: {"income": Decimal("0"), "expense": Decimal("0")})
    )
    categories = defaultdict(lambda: defaultdict(lambda: defaultdict(Decimal)))

    for row in fetch_report_splits(conn, year, currency):
        date_text = str(row["post_date"] or "")
        try:
            month = int(date_text[5:7])
        except (ValueError, IndexError):
            continue
        if month not in MONTHS:
            continue

        account_currency = row["currency"] or "UNKNOWN"
        account_name = fullnames.get(row["account_guid"], row["account_guid"])
        quantity = decimal_from_fraction(row["quantity_num"], row["quantity_denom"])

        if row["account_type"] == "INCOME":
            amount = -quantity
            monthly[account_currency][month]["income"] += amount
        else:
            amount = quantity
            monthly[account_currency][month]["expense"] += amount
        categories[account_currency][account_name][month] += amount

    return monthly, categories


def money(value: Decimal, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def print_summary(currency: str, by_month: dict, year: int) -> None:
    print("\n" + "=" * 80)
    print(f"INCOME / EXPENSE / CASH-FLOW REPORT — {year} — {currency}")
    print("=" * 80)
    print(f"{'Month':<12}{'Income':>21}{'Expenses':>21}{'Net cash flow':>24}")
    print("-" * 80)

    total_income = Decimal("0")
    total_expense = Decimal("0")
    for month in MONTHS:
        values = by_month.get(month, {"income": Decimal("0"), "expense": Decimal("0")})
        income = values["income"]
        expense = values["expense"]
        net = income - expense
        total_income += income
        total_expense += expense
        print(
            f"{calendar.month_abbr[month]:<12}"
            f"{money(income, currency):>21}"
            f"{money(expense, currency):>21}"
            f"{money(net, currency):>24}"
        )

    print("-" * 80)
    print(
        f"{'YEAR TOTAL':<12}"
        f"{money(total_income, currency):>21}"
        f"{money(total_expense, currency):>21}"
        f"{money(total_income - total_expense, currency):>24}"
    )


def print_category_breakdown(currency: str, categories: dict, year: int) -> None:
    print("\n" + "=" * 80)
    print(f"ACCOUNT / CATEGORY BREAKDOWN — {year} — {currency}")
    print("=" * 80)
    print(f"{'Account':<50}{'Annual total':>30}")
    print("-" * 80)

    totals = []
    for account, by_month in categories.items():
        total = sum(by_month.values(), Decimal("0"))
        totals.append((account, total))
    for account, total in sorted(totals, key=lambda item: abs(item[1]), reverse=True):
        label = account if len(account) <= 49 else account[:46] + "..."
        print(f"{label:<50}{money(total, currency):>30}")


def write_csv_report(path: str, all_monthly: dict, year: int) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["year", "currency", "month_number", "month", "income", "expenses", "net_cash_flow"]
        )
        for currency, by_month in sorted(all_monthly.items()):
            total_income = Decimal("0")
            total_expense = Decimal("0")
            for month in MONTHS:
                values = by_month.get(month, {"income": Decimal("0"), "expense": Decimal("0")})
                income = values["income"]
                expense = values["expense"]
                total_income += income
                total_expense += expense
                writer.writerow(
                    [
                        year,
                        currency,
                        month,
                        calendar.month_name[month],
                        str(income),
                        str(expense),
                        str(income - expense),
                    ]
                )
            writer.writerow(
                [
                    year,
                    currency,
                    "TOTAL",
                    "Year total",
                    str(total_income),
                    str(total_expense),
                    str(total_income - total_expense),
                ]
            )
    print(f"Wrote CSV report: {path}")


def make_plot(path: str, currency: str, by_month: dict, year: int) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plot generation requires matplotlib. Install it with: pip install matplotlib"
        ) from exc

    labels = [calendar.month_abbr[month] for month in MONTHS]
    income = [float(by_month.get(month, {}).get("income", Decimal("0"))) for month in MONTHS]
    expense = [float(by_month.get(month, {}).get("expense", Decimal("0"))) for month in MONTHS]
    net = [inc - exp for inc, exp in zip(income, expense)]
    positions = list(range(len(MONTHS)))
    width = 0.36

    figure, axes = plt.subplots(figsize=(13, 7))
    axes.bar([x - width / 2 for x in positions], income, width, label="Income", color="#2e8b57")
    axes.bar([x + width / 2 for x in positions], expense, width, label="Expenses", color="#c94c4c")
    axes.plot(positions, net, label="Net cash flow", color="#246eb9", marker="o", linewidth=2.2)
    axes.axhline(0, color="#333333", linewidth=0.8)
    axes.set_xticks(positions)
    axes.set_xticklabels(labels)
    axes.set_ylabel(currency)
    axes.set_title(f"Monthly Income, Expenses and Net Cash Flow — {year} — {currency}")
    axes.legend()
    axes.grid(axis="y", alpha=0.25)
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)
    print(f"Wrote cash-flow plot: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a monthly income, expense, and cash-flow report from a SQLite GnuCash book."
    )
    parser.add_argument("--gnucash-file", required=True, help="Path to a SQLite-backed .gnucash file")
    parser.add_argument("--year", required=True, type=int, help="Calendar year to report, e.g. 2026")
    parser.add_argument(
        "--currency",
        default=None,
        help="Optional currency mnemonic, e.g. GBP or EUR. Omit to report currencies separately.",
    )
    parser.add_argument("--detail", action="store_true", help="Print annual totals by income/expense account")
    parser.add_argument("--csv-output", default=None, help="Optional path for a monthly summary CSV")
    parser.add_argument(
        "--plot",
        default=None,
        help="Optional PNG output path for a cash-flow chart. Specify --currency when multiple currencies exist.",
    )
    args = parser.parse_args()

    if args.year < 1900 or args.year > 9999:
        parser.error("--year must be a four-digit calendar year")

    conn = open_readonly_database(args.gnucash_file)
    try:
        monthly, categories = make_report(conn, args.year, args.currency)
    finally:
        conn.close()

    if not monthly:
        print(f"No income or expense splits found for {args.year} in {args.currency or 'any currency'}.")
        return

    for currency in sorted(monthly):
        print_summary(currency, monthly[currency], args.year)
        if args.detail:
            print_category_breakdown(currency, categories[currency], args.year)

    if args.csv_output:
        write_csv_report(args.csv_output, monthly, args.year)

    if args.plot:
        if args.currency:
            selected_currency = args.currency.upper()
            if selected_currency not in monthly:
                raise RuntimeError(f"No {selected_currency} data available for {args.year}; cannot create plot")
        elif len(monthly) == 1:
            selected_currency = next(iter(monthly))
        else:
            available = ", ".join(sorted(monthly))
            raise RuntimeError(
                f"Multiple currencies found ({available}). Specify --currency to create a single-currency plot."
            )
        make_plot(args.plot, selected_currency, monthly[selected_currency], args.year)


if __name__ == "__main__":
    main()
