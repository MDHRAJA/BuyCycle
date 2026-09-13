# Buy or Wait?

An explainable, deterministic personal-finance agent. It reconstructs each
user's cash position, reserves known commitments, detects only supported weekly
or monthly recurring streams, converts dated foreign-currency events, and tests
every candidate plan against a 90-day minimum-balance forecast.

## Run

From the repository root, run:

```text
python -m pip install -r code/requirements.txt
python code/main.py
python code/evaluation/main.py
python code/dashboard.py
```

The first command reads only `dataset/` and writes root-level `output.csv`. The
second command enforces output schema, row count, value bounds, allowed enums,
and public-sample status/method diagnostics. No API key or network access is
needed.

## Decision policy

- Pending and scheduled debits are reserved; only scheduled/confirmed credits
  are counted. Failed, cancelled, and unrealized records are excluded.
- Recurrence needs at least two settled, comparable observations, with a stable
  weekly-to-monthly interval. This avoids turning one-off purchases into bills.
- Cash capacity is found by cent-level binary search, then every full, partial,
  and permitted installment candidate is replayed against the 90-day ledger.
- Methods respect the profile's accepted methods and installment limit; feasible
  plans are ranked by total cost, number of payments, then option ID.
- Image files are located and their absence never creates phantom cash. Events
  with blank amounts are read locally with OCR; only labelled financial totals
  such as net pay, balance due, and grand total are accepted.

`evaluation/usage_report.md` records the zero-model deterministic final run.

## Local review workspace

`dashboard.py` provides both a professional review workspace for the generated
predictions and a **New assessment** intake. The intake asks for a purchase
amount, available and reserve balances, confirmed income and date, essential
costs, pending debits, payment preference, installment terms (including fees),
and an optional completion deadline. It produces an on-screen 90-day decision
without changing the batch submission file.

The dashboard includes outcome summaries, one-click status filters, search,
payment plans, full-payment dates, and grounded decision explanations. It
listens only on localhost; set `PORT` to use another local port.

## Optional Gemini enrichment

Set `GEMINI_API_KEY` in the server environment (see the root `.env.example`) to
enable a server-side product-listing summary after a supported product link is
checked. The fallback sequence is Gemini 3.7 Flash, Gemini 3.6 Flash,
Gemini 2.5 Flash, then Gemini 3.1 Flash-Lite. The deterministic cash-flow
forecast remains the source of truth for affordability; Gemini is never used to
invent price history or financial facts.
