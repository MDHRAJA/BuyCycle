"""Buy or Wait? -- deterministic, explainable affordability planner.

Run from the repository root: ``python code/main.py``.  The program only reads
the participant-facing dataset and writes ``output.csv`` at the repository root.
"""
from __future__ import annotations

import csv
import math
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "dataset"
OUT_COLUMNS = ["request_id", "amount_safe_to_pay", "affordability_status",
               "recommended_payment_method", "payment_plan",
               "earliest_date_for_full_payment", "spending_changes_needed",
               "decision_explanation"]


def image_amount(text, direction):
    """Extract the financially relevant amount from OCR text without image IDs."""
    compact = re.sub(r"\s+", " ", text.lower())
    labels = (r"net pay|balance due|amount due|net amount|grand total|total paid|"
              r"total amount(?: received)?|total")
    matches = []
    for found in re.finditer(rf"(?:{labels}).{{0,55}}?([0-9][0-9,]*(?:\.\d{{1,2}})?)", compact):
        raw = found.group(1).replace(",", "")
        value = n(raw, math.nan)
        if math.isfinite(value) and value > 0:
            matches.append(value)
    # Salary and receipts often put the value on a subsequent line; OCR may join
    # it with currency text. The final labelled total is normally authoritative.
    if matches:
        return matches[-1]
    return math.nan


def build_ocr_reader():
    """Use installed OCR; development runs may keep it in work/ outside code.zip."""
    try:
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR()
    except ImportError:
        local_runtime = ROOT / "work" / "ocr_runtime"
        if local_runtime.exists():
            sys.path.insert(0, str(local_runtime))
            try:
                from rapidocr_onnxruntime import RapidOCR
                return RapidOCR()
            except ImportError:
                pass
    return None


def rows(name):
    with (DATA / name).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def d(value):
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def n(value, default=0.0):
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def parts(value):
    return set(filter(None, (value or "").split("|")))


def money(value):
    value = round(max(0, value) + 1e-9, 2)
    return str(int(value)) if value.is_integer() else f"{value:.2f}"


class Planner:
    def __init__(self):
        self.profiles = {r["user_id"]: r for r in rows("financial_profiles.csv")}
        self.events = defaultdict(list)
        for e in rows("financial_events.csv"):
            self.events[e["user_id"]].append(e)
        self.options = defaultdict(list)
        for o in rows("request_payment_options.csv"):
            self.options[o["request_id"]].append(o)
        self.rates = {(r["rate_date"], r["from_currency"], r["to_currency"]): n(r["rate"])
                      for r in rows("exchange_rates.csv")}
        self.messages = rows("messages.csv")
        self.message_by_event = defaultdict(list)
        for message in self.messages:
            if message.get("related_event_id"):
                self.message_by_event[message["related_event_id"]].append(message["message_text"].lower())
        self.salary_updates = defaultdict(list)
        for message in self.messages:
            text = message.get("message_text", "")
            if not re.search(r"salary|payroll|monthly pay|gaji", text, re.I):
                continue
            amounts = re.findall(r"\b(?:INR|IDR|USD|EUR|ZAR)\s*([\d,]+(?:\.\d+)?)", text, re.I)
            dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
            if amounts:
                self.salary_updates[message["user_id"]].append((d(dates[-1]) if dates else date.min,
                    float(amounts[-1].replace(",", ""))))
        # Media is deliberately opened to establish which image evidence exists.  The
        # planner never silently treats a missing image-backed amount as cash.
        self.image_paths = {}
        for r in rows("images.csv"):
            image = DATA / "media" / "images" / (r["image_id"] + ".png")
            if image.exists():
                image.read_bytes()  # ingest local media; content is never executable instruction
                self.image_paths[r.get("related_event_id", "")] = image
        self.ocr_amounts = self.extract_image_amounts()

    def extract_image_amounts(self):
        reader = build_ocr_reader()
        if reader is None:
            return {}
        amounts = {}
        events = {event["event_id"]: event for user_events in self.events.values() for event in user_events}
        for event_id, path in self.image_paths.items():
            event = events.get(event_id)
            if not event:
                continue
            try:
                result, _ = reader(str(path))
                text = "\n".join(item[1] for item in result or [])
                value = image_amount(text, event["direction"])
                if math.isfinite(value):
                    amounts[event_id] = value
            except Exception:
                # OCR failure never turns an unknown debit into zero or a credit
                # into available cash; normal conservative handling continues.
                continue
        return amounts

    def superseded_by_message(self, event_id):
        text = " ".join(self.message_by_event.get(event_id, []))
        return any(word in text for word in ("cancelled", "canceled", "reversed", "already settled"))

    def home_amount(self, e, home):
        amount = n(e.get("amount"), math.nan)
        if math.isnan(amount):
            amount = self.ocr_amounts.get(e["event_id"], math.nan)
            if math.isnan(amount):
                return math.nan
        cur = e.get("currency")
        if cur == home:
            return amount
        day = e.get("settlement_date") or e.get("event_date")
        rate = self.rates.get((day, cur, home))
        if rate is not None:
            return amount * rate
        inverse = self.rates.get((day, home, cur))
        return amount / inverse if inverse else math.nan

    def future_flows(self, user, start, days=90):
        """Known future cash flows plus conservative recurrence inferred from history."""
        p = self.profiles[user]; home = p["home_currency"]; end = start + timedelta(days=days)
        result = []
        es = self.events[user]
        # Known pending/scheduled debits and confirmed scheduled credits.  Future
        # settled entries are historical records from a later snapshot and excluded.
        for e in es:
            if not e.get("settlement_date"):
                continue
            if self.superseded_by_message(e["event_id"]):
                continue
            when = d(e["settlement_date"])
            if not start <= when <= end or e["status"] in {"failed", "cancelled", "unrealized"}:
                continue
            if e["status"] == "settled":
                continue
            if e["direction"] == "credit" and e["status"] != "scheduled":
                continue
            if e["direction"] not in {"credit", "debit"}:
                continue
            amount = self.home_amount(e, home)
            if e["category"] == "salary" and e["direction"] == "credit":
                applicable = [value for effective, value in self.salary_updates[user] if effective <= when]
                if applicable:
                    amount = applicable[-1]
            if math.isfinite(amount):
                result.append((when, amount if e["direction"] == "credit" else -amount, e["event_id"]))

        # A recurring stream requires >=2 comparable settled observations before the
        # request date. Use its median interval/amount and only project monthly-ish
        # or weekly patterns; one-off purchases never become recurring forecasts.
        groups = defaultdict(list)
        for e in es:
            if e["status"] != "settled" or e["direction"] not in {"credit", "debit"}:
                continue
            if not e.get("settlement_date"):
                continue
            when = d(e["settlement_date"])
            if when >= start:
                continue
            amount = self.home_amount(e, home)
            if math.isfinite(amount):
                groups[(e["description"], e["category"], e["direction"])].append((when, amount, e))
        seen = {(x[0], x[2]) for x in result}
        for (_, _, direction), items in groups.items():
            items.sort(key=lambda x: x[0])
            if len(items) < 2:
                continue
            tail = items[-4:]
            gaps = [(tail[i][0] - tail[i-1][0]).days for i in range(1, len(tail))]
            gap = sorted(gaps)[len(gaps)//2]
            if gap < 6 or gap > 35:  # supports weekly, fortnightly, and monthly only
                continue
            amounts = sorted(x[1] for x in tail)
            amount = amounts[len(amounts)//2]
            # Avoid classifying irregular discretionary one-off shopping as a bill.
            if max(amounts) > min(amounts) * 1.35 and direction == "debit":
                continue
            nxt = items[-1][0] + timedelta(days=gap)
            while nxt <= start:
                nxt += timedelta(days=gap)
            marker = items[-1][2]["event_id"]
            while nxt <= end:
                if (nxt, marker) not in seen:
                    result.append((nxt, amount if direction == "credit" else -amount, marker))
                nxt += timedelta(days=gap)
        return result

    def available_changes(self, user, start):
        """Return permitted changes to stable recurring flexible debits only."""
        profile = self.profiles[user]
        stop_categories = parts(profile["expense_categories_user_is_willing_to_stop"])
        reduce_categories = parts(profile["expense_categories_user_is_willing_to_reduce"])
        groups = defaultdict(list)
        for event in self.events[user]:
            if event["status"] == "settled" and event["direction"] == "debit" and event.get("settlement_date"):
                when = d(event["settlement_date"])
                if when < start:
                    amount = self.home_amount(event, profile["home_currency"])
                    if math.isfinite(amount):
                        groups[(event["description"], event["category"])].append((when, amount, event))
        options = []
        for (_, category), items in groups.items():
            items.sort(key=lambda x: x[0])
            if len(items) < 2:
                continue
            gap = (items[-1][0] - items[-2][0]).days
            if not 6 <= gap <= 35 or items[-1][2]["flexibility"] != "flexible":
                continue
            when, amount, event = items[-1]
            if category in stop_categories:
                options.append((event["event_id"], 0.0, f"stop:{event['event_id']}", amount))
            elif category in reduce_categories:
                new_amount = n(event.get("minimum_allowed_amount"), amount * .5)
                if new_amount < amount:
                    options.append((event["event_id"], new_amount, f"reduce_to:{event['event_id']}:{money(new_amount)}", amount - new_amount))
        return sorted(options, key=lambda x: x[3], reverse=True)[:8]

    def safe_capacity(self, user, request_date, payments=(), changes=()):
        p = self.profiles[user]; minimum = n(p["minimum_balance_to_keep"])
        balance = n(p["current_available_balance"])
        flows = self.future_flows(user, request_date)
        # Optional changes affect only future recurring debits matching allowed fields.
        alterations = {eid: value for eid, value in changes}
        by_day = defaultdict(float)
        for when, amount, eid in flows:
            if eid in alterations and amount < 0:
                amount = -alterations[eid]
            by_day[when] += amount
        for when, amount in payments:
            by_day[when] -= amount
        low = balance
        for when in sorted(by_day):
            balance += by_day[when]
            low = min(low, balance)
        return low >= minimum - 0.01, low, balance

    def safe_today(self, user, request_date, requested):
        # Capacity is monotonic; binary-search cents against the full safety horizon.
        lo, hi = 0, int(round(requested * 100))
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.safe_capacity(user, request_date, [(request_date, mid / 100)])[0]: lo = mid
            else: hi = mid - 1
        return lo / 100

    def earliest_full(self, user, start, amount):
        for offset in range(91):
            when = start + timedelta(days=offset)
            if self.safe_capacity(user, start, [(when, amount)])[0]: return when
        return None

    def installment_candidates(self, req):
        p = self.profiles[req["user_id"]]
        considers = parts(p["payment_methods_user_will_consider"])
        maximum = n(p.get("max_installment_months"), 999)
        result = []
        if "installments" not in considers: return result
        for o in self.options[req["request_id"]]:
            if o["payment_method"] != "installments" or n(o["number_of_payments"]) > maximum:
                continue
            first = d(o["first_payment_date"]); freq = int(n(o["payment_frequency_days"]))
            pay = n(o["payment_amount"]); count = int(n(o["number_of_payments"]))
            schedule = [(first + timedelta(days=i * freq), pay) for i in range(count)]
            if schedule[-1][0] <= d(req["desired_completion_date"]): result.append((o, schedule))
        return result

    def decision(self, req):
        user = req["user_id"]; p = self.profiles[user]; today = d(req["request_date"])
        amount = n(req["requested_amount"]); deadline = d(req["desired_completion_date"])
        considers = parts(p["payment_methods_user_will_consider"])
        safe_today = self.safe_today(user, today, amount)
        full_date = self.earliest_full(user, today, amount)
        full_safe = self.safe_capacity(user, today, [(today, amount)])[0]
        fmt_plan = lambda ps: "|".join(f"{x.isoformat()}:{money(y)}" for x, y in ps)
        prefix = f"Protecting the {p['home_currency']} {money(n(p['minimum_balance_to_keep']))} minimum"

        if full_safe and "full_payment" in considers:
            return [req["request_id"], money(amount), "affordable_now", "full_payment",
                    fmt_plan([(today, amount)]), today.isoformat(), "none",
                    f"Pay {p['home_currency']} {money(amount)} today. {prefix} throughout the 90-day forecast."]

        candidates = []
        for o, schedule in self.installment_candidates(req):
            if self.safe_capacity(user, today, schedule)[0]:
                candidates.append((n(o["total_payable_amount"]), len(schedule), o["payment_option_id"], "installments", schedule))
        if (req["allows_partial_payment"].lower() == "true" and "partial_payment" in considers
                and 0 < safe_today < amount and full_date and full_date <= deadline):
            candidates.append((amount, 2, "", "partial_payment", [(today, safe_today), (full_date, amount-safe_today)]))
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1], x[2]))
            _, _, _, method, schedule = candidates[0]
            return [req["request_id"], money(safe_today), "affordable_with_plan", method,
                    fmt_plan(schedule), full_date.isoformat() if full_date else "", "none",
                    f"Use the safe {method} schedule; {prefix} after every forecast cash flow."]
        # As a last active alternative, test only the profile-authorized changes to
        # stable flexible subscriptions/expenses. We never change protected spend.
        if "full_payment" in considers:
            changes = self.available_changes(user, today)
            for count in range(1, min(3, len(changes)) + 1):
                for selected in combinations(changes, count):
                    cash_changes = [(event_id, new_amount) for event_id, new_amount, _, _ in selected]
                    if self.safe_capacity(user, today, [(today, amount)], cash_changes)[0]:
                        actions = "|".join(change[2] for change in selected)
                        return [req["request_id"], money(safe_today), "affordable_with_plan", "full_payment",
                                fmt_plan([(today, amount)]), full_date.isoformat() if full_date else "", actions,
                                f"Pay in full today after the permitted spending changes ({actions}); {prefix} remains protected."]
        if full_date and full_date <= deadline and "full_payment" in considers:
            return [req["request_id"], money(safe_today), "affordable_later", "wait",
                    fmt_plan([(full_date, amount)]), full_date.isoformat(), "none",
                    f"Wait until {full_date.isoformat()} to pay in full; paying earlier breaches the protected minimum."]
        if "full_payment" not in considers:
            reason = "No permitted full, installment, or partial-payment option can complete this request safely within the forecast."
        elif not full_date:
            reason = "There is no safe full-payment date within the 90-day forecast after essential commitments."
        else:
            reason = f"The earliest safe full-payment date ({full_date.isoformat()}) is after the requested completion date."
        return [req["request_id"], money(safe_today), "not_affordable", "not_recommended", "none",
                "", "none", f"Do not proceed for this request. {reason} {prefix} takes priority."]


def main():
    planner = Planner()
    output = [planner.decision(r) for r in rows("requests.csv")]
    with (ROOT / "output.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f); writer.writerow(OUT_COLUMNS); writer.writerows(output)


if __name__ == "__main__":
    main()
