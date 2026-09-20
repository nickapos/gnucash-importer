"""CSV detection and parsers for supported bank and credit-card exports."""

import csv
from typing import List, Optional, Tuple

from config import (
    ALPHA_GR_HEADER_MARKER,
    ALPHA_GR_MAX_PREAMBLE_LINES,
    BASE_CURRENCY,
    BOS_TYPE_DESCRIPTIONS,
)


class AmountParseError(ValueError):
    """Raised when a CSV amount cell cannot be parsed into a number."""


def _parse_amount(raw, decimal_comma: bool = False) -> Optional[float]:
    """Parse an amount cell, returning ``None`` when it is not a number.

    Returning ``None`` (instead of silently substituting ``0.0``) lets the
    caller reject the row rather than importing a bogus zero-value
    transaction.
    """
    text = ("" if raw is None else str(raw)).strip()
    if not text:
        return None
    for symbol in ("£", "€", "$"):
        text = text.replace(symbol, "")
    text = text.strip()
    if "," in text and "." in text:
        # Both separators present: the last one is the decimal separator, so
        # "1.234,56" parses as 1234.56 and "1,234.56" as 1234.56 too.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif decimal_comma:
        # European style: 1.234,56 -> 1234.56
        text = text.replace(".", "").replace(",", ".")
    else:
        # UK/US style: 1,234.56 -> 1234.56
        text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


class BankFormatMapper:
    @staticmethod
    def map_revolut(row: dict) -> dict:
        raw_amount = row.get("Amount", row.get("amount", ""))
        amount = _parse_amount(raw_amount, decimal_comma=False)
        state = row.get("State", "")
        memo_parts = [part for part in [row.get("Type", ""), state] if part]
        return {
            "date": row.get("Completed Date") or row.get("Started Date") or row.get("date") or row.get("transaction_date", ""),
            "payee": row.get("Description") or row.get("counterparty") or row.get("description") or row.get("merchant_name", ""),
            "amount": amount,
            "amount_error": None if amount is not None else f"unparseable amount: {raw_amount!r}",
            "currency": (row.get("Currency") or row.get("currency", "")).upper() or BASE_CURRENCY,
            "memo": " / ".join(memo_parts) if memo_parts else (row.get("notes") or ""),
            "state": state,
            "is_pending": state.strip().upper() == "PENDING",
            "source_kind": "bank",
            "original_row": row,
        }

    @staticmethod
    def map_bof_scot(row: dict) -> dict:
        """Parse either known Bank of Scotland current-account CSV variant.

        Statement-style export:
          Date, Description, Type, Money In (£), Money Out (£), Balance (£)

        Online-banking transaction export:
          Transaction Date, Transaction Type, Sort Code, Account Number,
          Transaction Description, Debit Amount, Credit Amount, Balance
        """
        def clean_optional(raw) -> float:
            text = ("" if raw is None else str(raw)).strip()
            if not text or text.lower() == "blank":
                return 0.0
            value = _parse_amount(text, decimal_comma=False)
            if value is None:
                raise AmountParseError(f"unparseable amount: {text!r}")
            return value

        type_code = (row.get("Transaction Type") or row.get("Type") or "").strip().upper()
        date_value = row.get("Transaction Date") or row.get("Date") or row.get("Posting Date") or row.get("Value Date", "")
        payee = row.get("Transaction Description") or row.get("Description") or row.get("Payee", "")

        amount = None
        amount_error = None
        try:
            money_in = clean_optional(
                row.get("Credit Amount")
                or row.get("Money In (£)")
                or row.get("Money In")
                or row.get("Credit")
            )
            money_out = clean_optional(
                row.get("Debit Amount")
                or row.get("Money Out (£)")
                or row.get("Money Out")
                or row.get("Debit")
            )
            amount = money_in - money_out
        except AmountParseError as exc:
            amount_error = str(exc)

        return {
            "date": date_value,
            "payee": payee,
            "amount": amount,
            "amount_error": amount_error,
            "currency": BASE_CURRENCY,
            "memo": BOS_TYPE_DESCRIPTIONS.get(type_code, type_code) or row.get("Reference") or row.get("Notes", ""),
            "is_pending": False,
            "source_kind": "bank",
            "original_row": row,
        }

    @staticmethod
    def map_credit_card(row: dict) -> dict:
        """Parse the credit-card CSV layout:

          Transaction Date, Transaction Cleared Date, Transaction Type,
          Transaction Description, Transaction Amount

        This export represents ordinary card purchases as positive values.
        The importer uses a source-split convention where an outgoing bank
        payment is negative. A credit-card purchase increases the liability,
        so it is likewise converted to a negative source amount. Negative
        export values are therefore treated as credits/refunds and converted
        to positive amounts, reducing the card liability.
        """
        raw_amount = row.get("Transaction Amount") or ""
        exported_amount = _parse_amount(raw_amount, decimal_comma=False)
        amount = None if exported_amount is None else -exported_amount

        transaction_type = (row.get("Transaction Type") or "").strip()
        cleared_date = (row.get("Transaction Cleared Date") or "").strip()
        memo_parts = [part for part in [transaction_type, f"Cleared: {cleared_date}" if cleared_date else ""] if part]

        return {
            "date": row.get("Transaction Date") or "",
            "payee": row.get("Transaction Description") or "",
            "amount": amount,
            "amount_error": None if amount is not None else f"unparseable amount: {raw_amount!r}",
            "currency": BASE_CURRENCY,
            "memo": " / ".join(memo_parts),
            "cleared_date": cleared_date,
            "is_pending": not bool(cleared_date),
            "source_kind": "credit_card",
            "original_row": row,
        }

    @staticmethod
    def map_alpha_gr(row: dict) -> dict:
        raw_amount = row.get("Ποσό") or ""
        sign = (row.get("Πρόσημο ποσού") or "").strip().upper()
        # Alpha Bank exports use a dot as the decimal separator (100.00),
        # while still allowing the European comma style in the same column.
        amount = _parse_amount(raw_amount, decimal_comma=False)
        if amount is not None:
            if sign == "Χ":
                amount = -abs(amount)
            elif sign == "Π":
                amount = abs(amount)
        reference = row.get("Αρ. συναλλαγής") or ""
        return {
            "date": row.get("Ημ/νία") or "",
            "payee": row.get("Αιτιολογία") or "",
            "amount": amount,
            "amount_error": None if amount is not None else f"unparseable amount: {raw_amount!r}",
            "currency": "EUR",
            "memo": f"Ref: {reference}" if reference else "",
            "is_pending": False,
            "source_kind": "bank",
            "original_row": row,
        }

    @staticmethod
    def map_generic(row: dict) -> dict:
        lower = {key.lower().strip(): value for key, value in row.items()}
        amount = _parse_amount(lower.get("amount", ""), decimal_comma=False)
        return {
            "date": lower.get("date", ""),
            "payee": lower.get("payee", ""),
            "amount": amount,
            "amount_error": None if amount is not None else f"unparseable amount: {lower.get('amount', '')!r}",
            "currency": (lower.get("currency") or "").upper() or BASE_CURRENCY,
            "memo": lower.get("memo", ""),
            "is_pending": False,
            "source_kind": "bank",
            "original_row": row,
        }

    @staticmethod
    def detect_format(csv_file: str) -> str:
        try:
            with open(csv_file, "r", encoding="utf-8-sig", newline="") as handle:
                # Scan a generous window so Alpha Bank exports with a long
                # metadata preamble are still detected.
                for _ in range(ALPHA_GR_MAX_PREAMBLE_LINES):
                    line = handle.readline()
                    if not line:
                        break
                    if line.strip().startswith(ALPHA_GR_HEADER_MARKER):
                        return "alpha_gr"
                handle.seek(0)
                header = None
                for record in csv.reader(handle):
                    if any(str(cell).strip() for cell in record):
                        header = record
                        break
                if header is None:
                    return "generic"

            lower_header = [cell.strip().lower() for cell in header]
            original_header = [cell.strip() for cell in header]

            # Credit-card format has exactly one Transaction Amount column,
            # not current-account Debit Amount/Credit Amount columns.
            if (
                "transaction date" in lower_header
                and "transaction description" in lower_header
                and "transaction type" in lower_header
                and "transaction amount" in lower_header
                and "debit amount" not in lower_header
                and "credit amount" not in lower_header
            ):
                return "credit_card"

            if any("counterparty" in cell for cell in lower_header) or sum(
                signal in lower_header
                for signal in ("started date", "completed date", "product", "state")
            ) >= 2:
                return "revolut"

            if (
                "transaction date" in lower_header
                and "transaction description" in lower_header
                and "transaction type" in lower_header
                and ("debit amount" in lower_header or "credit amount" in lower_header)
            ):
                return "bof_scot"

            if any("posting date" == cell.lower() for cell in original_header) or any(
                "posting date" in cell for cell in lower_header
            ):
                return "bof_scot"
            if sum(
                any(signal in cell for signal in ("money in", "money out", "balance"))
                for cell in lower_header
            ) >= 2 and any("type" in cell for cell in lower_header):
                return "bof_scot"

            if all(any(required in cell for cell in lower_header) for required in ("date", "payee", "amount")):
                return "generic"
        except Exception as exc:
            print(f"Warning: could not detect bank format: {exc}")
        return "generic"


def _is_valid_mapped_transaction(mapped: dict) -> Tuple[bool, str]:
    if not str(mapped.get("date") or "").strip():
        return False, "missing transaction date"
    if not str(mapped.get("payee") or "").strip():
        return False, "missing transaction description/payee"
    if mapped.get("amount") is None:
        return False, mapped.get("amount_error") or "missing transaction amount"
    return True, ""


_MAPPERS = {
    "revolut": BankFormatMapper.map_revolut,
    "bof_scot": BankFormatMapper.map_bof_scot,
    "credit_card": BankFormatMapper.map_credit_card,
    "alpha_gr": BankFormatMapper.map_alpha_gr,
    "generic": BankFormatMapper.map_generic,
}


def map_row(fmt: str, row: dict) -> dict:
    """Map a raw CSV row using the mapper for ``fmt``.

    Shared by the importer and the validation tool so every bank format is
    handled in exactly one place.
    """
    mapper = _MAPPERS.get(fmt, BankFormatMapper.map_generic)
    return mapper(row)


def read_bank_csv(csv_file: str, include_pending: bool = False) -> Tuple[str, List[dict]]:
    mapper = BankFormatMapper()
    fmt = mapper.detect_format(csv_file)
    delimiter = ";" if fmt == "alpha_gr" else ","

    rows: List[dict] = []
    skipped_pending = 0
    skipped_invalid = 0
    header = None
    physical_line = 0

    # Stream the file instead of loading it entirely into memory.
    with open(csv_file, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        for record in reader:
            physical_line += 1
            if not any(str(cell).strip() for cell in record):
                continue
            if fmt == "alpha_gr":
                # Skip the metadata preamble lines; only the Alpha Bank header
                # row (starting with the marker) begins the transaction table.
                if str(record[0]).strip().startswith(ALPHA_GR_HEADER_MARKER):
                    header = [cell.strip() for cell in record]
                    break
                continue
            # Every other format: the first non-empty line is the header.
            header = [cell.strip() for cell in record]
            break

        if header is None:
            return fmt, rows

        for record in reader:
            physical_line += 1
            row = dict(zip(header, record))
            if not any(str(value).strip() for value in row.values()):
                continue

            mapped = map_row(fmt, row)

            valid, reason = _is_valid_mapped_transaction(mapped)
            if not valid:
                skipped_invalid += 1
                print(f"Skipping malformed CSV row {physical_line}: {reason}")
                continue
            if mapped["is_pending"] and not include_pending:
                skipped_pending += 1
                continue

            mapped["row_number"] = physical_line
            rows.append(mapped)

    if skipped_pending:
        print(f"Skipped {skipped_pending} pending transaction(s); use --include-pending to include them")
    if skipped_invalid:
        print(f"Skipped {skipped_invalid} malformed/unmapped CSV row(s)")
    return fmt, rows
