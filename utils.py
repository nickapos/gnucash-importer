"""Shared parsing, text normalization, and terminal helpers."""

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from typing import List, Optional, Set

from config import IGNORED_WORDS


def parse_tx_date(date_str: str) -> datetime:
    date_str = (date_str or "").strip()
    if not date_str:
        raise ValueError("empty transaction date")

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
        "%d %b %y",
        "%d %b %Y",
        "%d %B %y",
        "%d %B %Y",
        "%d-%b-%y",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
        "%d-%m-%Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"unsupported transaction date: {date_str!r}")


def normalize_payee(value: str) -> str:
    """Normalize bank descriptions for duplicate/fuzzy matching."""
    value = (value or "").lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def compute_tx_hash(tx: dict) -> str:
    """Stable content hash for a mapped CSV row.

    Shared by the importer and the validation tool so both always agree on
    which rows have already been handled.
    """
    raw = f"{tx['date']}|{tx['payee']}|{float(tx['amount']):.2f}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def tokenize(text: str) -> Set[str]:
    """Split text into lowercased meaningful words, dropping stop words.

    ``[^\\w\\s]`` (with re.UNICODE) keeps accented and non-Latin letters,
    mirroring ``normalize_payee`` so Greek (Alpha Bank) and other localized
    payees are never silently reduced to an empty token set.
    """
    cleaned = re.sub(r"[^\w\s]", " ", (text or "").lower(), flags=re.UNICODE)
    return {
        word
        for word in cleaned.split()
        if len(word) > 2 and word not in IGNORED_WORDS
    }


def atomic_write_json(path: str, data) -> None:
    """Write JSON to ``path`` atomically (temp file + os.replace)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def is_real_asset_account(account) -> bool:
    """Exclude GnuCash template and orphan bookkeeping accounts."""
    commodity = getattr(account, "commodity", None)
    if commodity is not None and getattr(commodity, "namespace", "") == "template":
        return False
    return not account.fullname.split(":")[-1].startswith("Orphan-")


class AccountPathCompleter:
    def __init__(self, account_fullnames: List[str]):
        self.account_fullnames = sorted(set(account_fullnames))

    def complete(self, text: str, state: int) -> Optional[str]:
        matches = (
            self.account_fullnames
            if not text
            else [name for name in self.account_fullnames if name.lower().startswith(text.lower())]
        )
        return matches[state] if state < len(matches) else None


def input_with_completion(prompt: str, completer: AccountPathCompleter) -> str:
    try:
        import readline

        old_completer = readline.get_completer()
        old_delims = readline.get_completer_delims()
        readline.set_completer(completer.complete)
        readline.set_completer_delims("")
        readline.parse_and_bind("tab: complete")
        try:
            return input(prompt)
        finally:
            readline.set_completer(old_completer)
            readline.set_completer_delims(old_delims)
    except (ImportError, AttributeError):
        return input(prompt)
