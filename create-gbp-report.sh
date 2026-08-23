python ./income_expense_report.py \
  --gnucash-file="portfolio-sqlite.gnucash" \
  --year=2026 \
  --report-currency=GBP \
  --rate-tolerance-days=7 \
  --detail \
  --conversion-audit-csv="conversion-audit-2026-gbp.csv" \
  --plot="cashflow-2026-gbp.png"
