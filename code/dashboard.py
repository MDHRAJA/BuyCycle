"""Local, read-only decision dashboard for reviewing output.csv.

Run: python code/dashboard.py
Open: http://localhost:8000
"""
import csv
import html
import json
import os
import re
from collections import Counter
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def as_number(value):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def display_number(value):
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def assess_manual(data):
    """Evaluate every offered payment path over a 90-day, reserve-protected ledger."""
    requested = as_number(data.get("requested_amount"))
    balance = as_number(data.get("current_balance"))
    minimum = as_number(data.get("minimum_balance"))
    income = as_number(data.get("confirmed_monthly_income"))
    essentials = as_number(data.get("essential_monthly_expenses"))
    flexible = as_number(data.get("flexible_monthly_spending"))
    pending = as_number(data.get("pending_debits"))
    reserved = as_number(data.get("reserved_balance"))
    reduction = min(flexible, as_number(data.get("flexible_reduction")))
    currency = re.sub(r"[^A-Z]", "", (data.get("currency") or "USD").upper())[:3] or "USD"
    preference = data.get("payment_preference") or "best_option"
    installments = max(2, min(24, int(as_number(data.get("installment_count")) or 3)))
    total_payable = as_number(data.get("installment_total")) or requested
    today, horizon = date.today(), date.today() + timedelta(days=90)

    def parse_day(name):
        try:
            return date.fromisoformat(data.get(name, ""))
        except ValueError:
            return None

    income_date, deadline = parse_day("income_date"), parse_day("desired_completion_date")
    if income_date:
        while income_date < today:
            income_date += timedelta(days=30)

    def baseline(cut=0):
        events = [(today, -(pending + reserved), "pending and earmarked funds")]
        monthly_outflow = essentials + flexible - cut
        for month in range(1, 4):
            events.append((today + timedelta(days=30 * month), -monthly_outflow, "monthly spending"))
            if income_date:
                payday = income_date + timedelta(days=30 * (month - 1))
                if payday <= horizon:
                    events.append((payday, income, "confirmed income"))
        return events

    def forecast(candidate, cut=0):
        # On the same day, known income is credited before a proposed payment.
        events = baseline(cut) + [(when, -amount, "purchase payment") for when, amount in candidate]
        events.sort(key=lambda event: (event[0], 1 if event[2] == "purchase payment" else 0))
        running = balance
        floor = balance
        for _, amount, _ in events:
            running += amount
            floor = min(floor, running)
        return floor

    safe_today = max(0, min(requested, forecast([]) - minimum))
    baseline_floor = forecast([])
    monthly_gap = max(0, essentials + flexible - reduction - income)
    candidates = []

    def add(method, status, payments, cut=0):
        if not payments or payments[-1][0] > horizon or (deadline and payments[-1][0] > deadline):
            return
        if forecast(payments, cut) >= minimum:
            candidates.append({"method": method, "status": status, "payments": payments, "cut": cut})

    add("full_payment", "affordable_now", [(today, requested)])
    installment_payments = [(today + timedelta(days=30 * n), total_payable / installments) for n in range(installments)]
    add("installments", "affordable_with_plan", installment_payments)
    if income_date:
        add("wait", "affordable_later", [(income_date, requested)])
        if 0 < safe_today < requested:
            add("partial_payment", "affordable_with_plan", [(today, safe_today), (income_date, requested - safe_today)])

    # A spending adjustment is only proposed when it changes an otherwise unsafe result.
    if not candidates and reduction:
        add("full_payment", "affordable_now", [(today, requested)], reduction)
        add("installments", "affordable_with_plan", installment_payments, reduction)
        if income_date:
            add("wait", "affordable_later", [(income_date, requested)], reduction)
            if 0 < safe_today < requested:
                add("partial_payment", "affordable_with_plan", [(today, safe_today), (income_date, requested - safe_today)], reduction)

    preferred = [candidate for candidate in candidates if candidate["method"] == preference]
    preference_blocked = preference != "best_option" and not preferred and bool(candidates)
    pool = candidates if preference == "best_option" else preferred
    method_rank = {"full_payment": 0, "partial_payment": 1, "installments": 2, "wait": 3}
    if pool:
        selected = min(pool, key=lambda candidate: (candidate["cut"] > 0, candidate["payments"][-1][0], method_rank[candidate["method"]], sum(amount for _, amount in candidate["payments"])))
        plan, method, status = selected["payments"], selected["method"], selected["status"]
        changes = (f"Reduce flexible spending by {currency} {display_number(selected['cut'])} per month for 90 days."
                   if selected["cut"] else "None required.")
        decision = (f"This {method.replace('_', ' ')} plan keeps the balance at or above the {currency} {display_number(minimum)} reserve throughout the forecast."
                    + (f" It requires reducing flexible spending by {currency} {display_number(selected['cut'])} each month." if selected["cut"] else ""))
    else:
        plan, method, status = [], "not_recommended", "not_affordable"
        if preference_blocked:
            changes = f"Choose a different payment method or adjust the plan; the requested {preference.replace('_', ' ')} option is not safe within the forecast."
            decision = f"A safer method exists, but it does not match the payment method you selected. No substitution was made."
        elif baseline_floor < minimum:
            shortfall = minimum - baseline_floor
            changes = (f"Close the {currency} {display_number(monthly_gap)} monthly gap with lower spending or confirmed income; "
                       f"the 90-day baseline is short of the reserve by {currency} {display_number(shortfall)}.")
            decision = (f"The purchase is small relative to today's balance, but the 90-day baseline reaches {currency} {display_number(baseline_floor)} "
                        f"even before this purchase—below the {currency} {display_number(minimum)} reserve. Stabilize the monthly cash-flow gap first.")
        else:
            changes = (f"Even reducing flexible spending by {currency} {display_number(reduction)} per month does not make a full plan safe."
                       if reduction else "No safe plan is available from the confirmed information provided.")
            decision = f"The requested purchase would breach the {currency} {display_number(minimum)} reserve or cannot be completed within the forecast."

    payment_plan = "|".join(f"{when.isoformat()}:{display_number(amount)}" for when, amount in plan) or "none"
    paid_today = sum(amount for when, amount in plan if when == today)
    parameter_audit = {
        "available_balance": display_number(balance),
        "pending_debits": display_number(pending),
        "earmarked_funds": display_number(reserved),
        "essential_monthly_expenses": display_number(essentials),
        "confirmed_monthly_income": display_number(income),
        "flexible_monthly_spending": display_number(flexible),
        "accepted_flexible_reduction": display_number(reduction),
        "payment_preference": preference,
        "installment_count": installments,
        "installment_total": display_number(total_payable),
        "protected_minimum": display_number(minimum),
        "forecast_days": 90,
    }
    return {"amount_safe_to_pay": display_number(safe_today), "affordability_status": status,
            "recommended_payment_method": method, "payment_plan": payment_plan,
            "earliest_date_for_full_payment": plan[-1][0].isoformat() if plan else "Not within the current 90-day forecast",
            "spending_changes_needed": changes,
            "decision_explanation": decision,
            "baseline_floor": display_number(baseline_floor),
            "baseline_reserve_gap": display_number(max(0, minimum - baseline_floor)),
            "monthly_cash_flow_gap": display_number(monthly_gap),
            "remaining_balance_today": display_number(balance - pending - reserved - paid_today),
            "remaining_balance_if_paid_today": display_number(balance - pending - reserved - requested),
            "parameter_audit": parameter_audit}


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def page():
    requests = {row["request_id"]: row for row in read_csv(ROOT / "dataset" / "requests.csv")}
    profiles = {row["user_id"]: row for row in read_csv(ROOT / "dataset" / "financial_profiles.csv")}
    decisions = read_csv(ROOT / "output.csv")
    counts = Counter(row["affordability_status"] for row in decisions)
    labels = {"affordable_now": "Pay today", "affordable_with_plan": "Safe plan",
              "affordable_later": "Wait", "not_affordable": "Hold off"}
    card_order = ["affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"]
    cards = "".join(f'''<button class="metric {status}" data-status="{status}">
        <span>{labels[status]}</span><strong>{counts[status]}</strong><small>of {len(decisions)} requests</small></button>'''
                    for status in card_order)
    rows = []
    for decision in decisions:
        request = requests[decision["request_id"]]; profile = profiles[request["user_id"]]
        search = " ".join(decision.values()) + " " + request["request_text"]
        status = decision["affordability_status"]
        rows.append(f'''<tr data-status="{status}" data-search="{html.escape(search.lower(), quote=True)}">
          <td><b>{html.escape(decision['request_id'])}</b><small>{html.escape(request['request_type'].replace('_', ' '))}</small></td>
          <td><span class="tag {status}">{html.escape(labels[status])}</span></td>
          <td><b>{html.escape(profile['home_currency'])} {html.escape(decision['amount_safe_to_pay'])}</b><small>safe to pay today</small></td>
          <td class="method">{html.escape(decision['recommended_payment_method'].replace('_', ' '))}</td>
          <td class="plan">{html.escape(decision['payment_plan'])}</td>
          <td>{html.escape(decision['earliest_date_for_full_payment']) or '—'}</td>
          <td class="reason">{html.escape(decision['decision_explanation'])}</td></tr>''')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Buy or Wait? | Decision Review</title>
    <style>
    :root{{--ink:#172033;--muted:#647089;--line:#e4e8f0;--paper:#fff;--canvas:#f4f6fa;--navy:#101a31;--blue:#315cf4}}
    *{{box-sizing:border-box}}body{{margin:0;background:var(--canvas);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:15px}}
    header{{background:var(--navy);color:white;padding:25px max(24px,calc((100vw - 1440px)/2));display:flex;justify-content:space-between;gap:24px;align-items:center}}.brand{{display:flex;gap:12px;align-items:center}}.mark{{width:36px;height:36px;display:grid;place-items:center;border-radius:11px;background:linear-gradient(135deg,#6da0ff,#3359ed);font-weight:800;font-size:20px}}h1{{font-size:20px;letter-spacing:-.02em;margin:0}}.sub{{color:#bdc9e0;font-size:13px;margin-top:3px}}.badge{{font-size:13px;padding:8px 11px;background:#1c2945;border:1px solid #344361;border-radius:99px;white-space:nowrap}}
    main{{max-width:1440px;margin:auto;padding:30px 24px 48px}}.eyebrow{{font-size:12px;text-transform:uppercase;letter-spacing:.1em;font-weight:800;color:#6b7892;margin-bottom:9px}}h2{{font-size:30px;letter-spacing:-.045em;margin:0 0 7px}}.intro{{margin:0;color:var(--muted);max-width:690px;line-height:1.55}}
    .metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:26px 0}}.metric{{appearance:none;border:1px solid var(--line);text-align:left;background:var(--paper);border-radius:16px;padding:17px 18px;cursor:pointer;transition:.16s transform,.16s box-shadow}}.metric:hover,.metric.active{{transform:translateY(-2px);box-shadow:0 10px 24px #16233a12;border-color:#b6c2dc}}.metric span,.metric small{{display:block;color:var(--muted)}}.metric span{{font-weight:650;font-size:13px}}.metric strong{{display:block;font-size:30px;letter-spacing:-.04em;margin:6px 0 2px}}.metric small{{font-size:12px}}.metric.affordable_now strong{{color:#14804a}}.metric.affordable_with_plan strong{{color:#315cf4}}.metric.affordable_later strong{{color:#a56b00}}.metric.not_affordable strong{{color:#c83b3b}}
    .workspace{{background:var(--paper);border:1px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:0 12px 32px #1720330b}}.toolbar{{padding:18px;border-bottom:1px solid var(--line);display:flex;gap:14px;justify-content:space-between;align-items:center}}.toolbar h3{{font-size:16px;margin:0}}.toolbar p{{color:var(--muted);font-size:13px;margin:3px 0 0}}.search{{display:flex;align-items:center;gap:8px;border:1px solid #ccd4e3;border-radius:10px;padding:0 10px;min-width:330px}}.search span{{color:#8090ab}}input{{border:0;outline:0;padding:10px 2px;width:100%;font:inherit;color:var(--ink)}}.table-wrap{{overflow:auto;max-height:67vh}}table{{width:100%;border-collapse:collapse;min-width:1070px}}th{{position:sticky;top:0;background:#fafbfe;z-index:1;text-align:left;color:#66738b;font-size:11px;letter-spacing:.07em;text-transform:uppercase;padding:12px 16px;border-bottom:1px solid var(--line)}}td{{padding:15px 16px;border-bottom:1px solid #edf0f5;vertical-align:top;line-height:1.45}}tr:last-child td{{border:0}}tbody tr:hover{{background:#fbfcff}}td b{{display:block;font-size:14px}}td small{{display:block;color:var(--muted);font-size:12px;margin-top:2px}}.tag{{display:inline-block;padding:5px 9px;border-radius:99px;font-size:12px;font-weight:700;white-space:nowrap}}.affordable_now .tag,.tag.affordable_now{{background:#e1f8e9;color:#087540}}.affordable_with_plan .tag,.tag.affordable_with_plan{{background:#e7edff;color:#294cb5}}.affordable_later .tag,.tag.affordable_later{{background:#fff3d8;color:#946100}}.not_affordable .tag,.tag.not_affordable{{background:#ffe6e6;color:#ae3030}}.method{{font-weight:650;text-transform:capitalize}}.plan{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#43516b;max-width:230px}}.reason{{color:#43516b;max-width:360px;font-size:13px}}.empty{{display:none;padding:38px;text-align:center;color:var(--muted)}}
    .assessor{{display:grid;grid-template-columns:1.2fr .8fr;gap:0;background:#111d36;border-radius:18px;overflow:hidden;margin:26px 0;box-shadow:0 14px 30px #17203319}}.assessment-copy{{padding:27px;color:white}}.assessment-copy h3{{font-size:21px;letter-spacing:-.03em;margin:6px 0}}.assessment-copy p{{color:#c6d2eb;line-height:1.55;margin:0}}.assessment-copy .eyebrow{{color:#92a9dc}}form{{padding:20px;background:white;display:grid;grid-template-columns:repeat(2,1fr);gap:12px}}.field{{display:flex;flex-direction:column;gap:5px}}.field.wide{{grid-column:span 2}}.field label{{font-size:12px;font-weight:750;color:#53617a}}.field input,.field select{{border:1px solid #d5dce8;border-radius:8px;padding:9px 10px;font:inherit;background:white}}.field input:focus,.field select:focus{{outline:2px solid #b8c7ff;border-color:#5978f7}}button.primary{{border:0;border-radius:9px;background:var(--blue);color:white;font:700 14px inherit;padding:11px 14px;cursor:pointer}}button.primary:hover{{background:#244be0}}.assessment-result{{display:none;grid-column:span 2;border-radius:10px;padding:13px 14px;background:#f1f5ff;color:#263a78;font-size:13px;line-height:1.5}}.assessment-result strong{{font-size:15px;text-transform:capitalize}}.note{{font-size:12px!important;color:#9eacce!important;margin-top:18px!important}}
    @media(max-width:820px){{header{{padding:18px 20px}}.badge{{display:none}}main{{padding:24px 16px}}h2{{font-size:26px}}.metrics{{grid-template-columns:repeat(2,1fr);gap:10px}}.assessor{{grid-template-columns:1fr}}form{{grid-template-columns:1fr}}.field.wide,.assessment-result{{grid-column:span 1}}.toolbar{{align-items:stretch;flex-direction:column}}.search{{min-width:0}}.table-wrap{{max-height:none}}}}
    </style></head><body><header><div class="brand"><div class="mark">↗</div><div><h1>Buy or Wait?</h1><div class="sub">Affordability decision workspace</div></div></div><div class="badge">90-day safety forecast · {len(decisions)} requests</div></header><main>
    <div class="eyebrow">Decision overview</div><h2>Clear financial decisions, ready to review.</h2><p class="intro">Each recommendation protects the user’s minimum balance while accounting for upcoming commitments, confirmed income, and permitted payment choices.</p>
    <section class="assessor"><div class="assessment-copy"><div class="eyebrow">New assessment</div><h3>Can I afford this?</h3><p>Choose the best safe way to buy, not merely what fits in today’s account balance. The forecast protects your reserve for the next 90 days.</p><p class="note">Use confirmed amounts only. This is a planning tool, not financial, legal, or investment advice.</p></div><form id="assessment-form"><div class="field"><label>Purchase amount</label><input required name="requested_amount" type="number" min="0" step="0.01" placeholder="e.g. 1200"></div><div class="field"><label>Currency</label><input required name="currency" maxlength="3" value="USD"></div><div class="field"><label>Available balance</label><input required name="current_balance" type="number" min="0" step="0.01"></div><div class="field"><label>Minimum balance to keep</label><input required name="minimum_balance" type="number" min="0" step="0.01"></div><div class="field"><label>Confirmed monthly income</label><input required name="confirmed_monthly_income" type="number" min="0" step="0.01"></div><div class="field"><label>Next confirmed income date</label><input name="income_date" type="date"></div><div class="field"><label>Essential monthly expenses</label><input required name="essential_monthly_expenses" type="number" min="0" step="0.01"></div><div class="field"><label>Flexible monthly spending</label><input name="flexible_monthly_spending" type="number" min="0" step="0.01" value="0"></div><div class="field"><label>Pending debits</label><input required name="pending_debits" type="number" min="0" step="0.01" value="0"></div><div class="field"><label>Flexible reduction you accept</label><input name="flexible_reduction" type="number" min="0" step="0.01" value="0"></div><div class="field"><label>Payment preference</label><select name="payment_preference"><option value="best_option">Recommend safest option</option><option value="full_payment">Pay in full</option><option value="installments">Use installments</option><option value="partial_payment">Make a partial payment</option></select></div><div class="field"><label>Installment count if applicable</label><input name="installment_count" type="number" min="2" max="24" value="3"></div><div class="field"><label>Total payable with fees</label><input name="installment_total" type="number" min="0" step="0.01" placeholder="Purchase amount if no fees"></div><div class="field"><label>Complete purchase by (optional)</label><input name="desired_completion_date" type="date"></div><div class="field wide"><button class="primary" type="submit">Find the safest plan</button></div><output class="assessment-result" id="assessment-result"></output></form></section><section class="metrics">{cards}</section><section class="workspace"><div class="toolbar"><div><h3>Batch decisions</h3><p id="result-count">Showing {len(decisions)} requests</p></div><label class="search"><span>⌕</span><input id="q" aria-label="Search decisions" placeholder="Search request, plan, or recommendation"></label></div>
    <div class="table-wrap"><table><thead><tr><th>Request</th><th>Decision</th><th>Safe today</th><th>Recommended method</th><th>Payment plan</th><th>Full-payment date</th><th>Why</th></tr></thead><tbody>{''.join(rows)}</tbody></table><div class="empty" id="empty">No decisions match this filter.</div></div></section>
    </main><script>const q=document.querySelector('#q'),cards=[...document.querySelectorAll('.metric')],trs=[...document.querySelectorAll('tbody tr')],count=document.querySelector('#result-count'),empty=document.querySelector('#empty');let status='';function filter(){{let shown=0,query=q.value.toLowerCase();trs.forEach(r=>{{let match=(!status||r.dataset.status===status)&&r.dataset.search.includes(query);r.hidden=!match;if(match)shown++}});count.textContent=`Showing ${{shown}} request${{shown===1?'':'s'}}`;empty.style.display=shown?'none':'block'}}q.oninput=filter;cards.forEach(card=>card.onclick=()=>{{status=status===card.dataset.status?'':card.dataset.status;cards.forEach(c=>c.classList.toggle('active',c.dataset.status===status));filter()}});document.querySelector('#assessment-form').onsubmit=async e=>{{e.preventDefault();const out=document.querySelector('#assessment-result');out.style.display='block';out.textContent='Checking the 90-day forecast…';try{{const r=await fetch('/api/assess',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))}});if(!r.ok)throw new Error();const d=await r.json();out.replaceChildren();const title=document.createElement('strong');title.textContent=`${{d.recommended_payment_method.replace('_',' ')}} · ${{d.affordability_status.replaceAll('_',' ')}}`;out.append(title,document.createElement('br'),`Safe today: ${{d.amount_safe_to_pay}} · Full-payment date: ${{d.earliest_date_for_full_payment}}`,document.createElement('br'),d.decision_explanation);}}catch{{out.textContent='The assessment could not be completed. Please try again.'}}}};</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/", "/index.html"}:
            self.send_error(404); return
        content = page().encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content)
    def do_POST(self):
        if self.path != "/api/assess":
            self.send_error(404); return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 16_384:
                self.send_error(413, "Assessment payload is too large"); return
            payload = json.loads(self.rfile.read(size))
            response = json.dumps(assess_manual(payload)).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response))); self.end_headers(); self.wfile.write(response)
        except (ValueError, json.JSONDecodeError):
            self.send_error(400, "Invalid assessment data")
    def log_message(self, *_):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Dashboard running at http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
