"""Account cache, currency filtering, and suggestion generation."""

from typing import Optional, Set


class AccountMatcher:
    def __init__(self, book, payee_mapper, history_analyzer):
        self.book = book
        self.payee_mapper = payee_mapper
        self.history_analyzer = history_analyzer
        self.accounts_cache = {account.guid: account for account in book.accounts}

    @staticmethod
    def _acct_currency_matches(account, currency: str) -> bool:
        commodity = getattr(account, "commodity", None)
        return bool(
            commodity
            and commodity.namespace == "CURRENCY"
            and commodity.mnemonic.upper() == currency.upper()
        )

    @staticmethod
    def _tokens(text: str) -> Set[str]:
        import re
        text = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
        ignored = {"the", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by"}
        return {word for word in text.split() if len(word) > 2 and word not in ignored}

    def get_account_by_guid(self, guid: str):
        return self.accounts_cache.get(guid)

    def get_account_guid(self, name: str) -> Optional[str]:
        for account in self.accounts_cache.values():
            if account.name == name or account.fullname == name:
                return account.guid
        return None

    def find_matching_accounts(self, payee, description="", transaction_currency="GBP", max_suggestions=5, exclude_guid=None):
        suggestions = []
        mapping = self.payee_mapper.get_mapping(payee)
        if mapping:
            account = self.get_account_by_guid(mapping["account_guid"])
            if account and self._acct_currency_matches(account, transaction_currency):
                suggestions.append((account, 1.0, "Manual mapping"))

        for fullname, confidence, reason in self.history_analyzer.suggest_mapping(payee, description):
            account = next((a for a in self.accounts_cache.values() if a.fullname == fullname), None)
            if account and self._acct_currency_matches(account, transaction_currency):
                suggestions.append((account, confidence, f"History: {reason}"))

        payee_tokens = self._tokens(payee)
        payee_lower = payee.lower().strip()
        for account in self.accounts_cache.values():
            if not self._acct_currency_matches(account, transaction_currency):
                continue
            name_lower = account.name.lower().strip()
            full_lower = account.fullname.lower().strip()
            if payee_lower == name_lower or payee_lower == full_lower:
                suggestions.append((account, 0.95, "Exact account name match"))
                continue
            if payee_lower in name_lower or payee_lower in full_lower:
                suggestions.append((account, 0.8, "Account name contains payee"))
                continue
            overlap = payee_tokens & self._tokens(account.fullname.replace(":", " "))
            if overlap:
                score = len(overlap) / max(len(payee_tokens), 1)
                if any(len(word) >= 5 for word in overlap):
                    score = min(score + 0.15, 0.9)
                suggestions.append((account, score, f"Keyword match: {', '.join(sorted(overlap))}"))

        if exclude_guid:
            suggestions = [item for item in suggestions if item[0].guid != exclude_guid]

        best = {}
        for account, confidence, reason in suggestions:
            if account.guid not in best or confidence > best[account.guid][0]:
                best[account.guid] = (confidence, reason, account)
        return sorted(
            [(account, confidence, reason) for account, (confidence, reason, account) in best.items()],
            key=lambda item: item[1],
            reverse=True,
        )[:max_suggestions]

    @staticmethod
    def suggest_category(payee: str) -> str:
        text = payee.lower()
        if any(word in text for word in ["tesco", "sainsbury", "asda", "supermarket", "aldi", "lidl"]):
            return "Expenses:Food"
        if any(word in text for word in ["amazon", "ebay", "shop", "retail", "argos"]):
            return "Expenses:Shopping"
        if any(word in text for word in ["shell", "bp", "esso", "petrol", "fuel", "uber", "train", "bus"]):
            return "Expenses:Transportation"
        if any(word in text for word in ["netflix", "spotify", "subscription", "disney", "prime"]):
            return "Expenses:Subscriptions"
        if any(word in text for word in ["salary", "wages", "payroll", "hmrc"]):
            return "Income:Salary"
        return "Expenses:Miscellaneous"
