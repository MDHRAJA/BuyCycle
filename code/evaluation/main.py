"""Contract and sample-set evaluation for Buy or Wait?"""
import csv
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "code"))
from main import OUT_COLUMNS, Planner, n, rows  # noqa: E402
from dashboard import assess_manual  # noqa: E402
from decision_agent import run_assessment_agent  # noqa: E402


def validate(predictions, requests):
    by_id = {r["request_id"]: r for r in requests}
    assert len(predictions) == len(requests) == len({p["request_id"] for p in predictions})
    assert list(predictions[0]) == OUT_COLUMNS
    for p in predictions:
        request = by_id[p["request_id"]]
        assert 0 <= n(p["amount_safe_to_pay"]) <= n(request["requested_amount"])
        assert p["affordability_status"] in {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
        assert p["recommended_payment_method"] in {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
        if p["affordability_status"] == "affordable_now":
            assert p["earliest_date_for_full_payment"] == request["request_date"]
        if p["recommended_payment_method"] == "not_recommended":
            assert p["payment_plan"] == "none"


def validate_assessor_controls():
    """Regression checks for every optional interactive assessment control."""
    base = {"requested_amount": "10000", "current_balance": "20000", "minimum_balance": "5000",
            "essential_monthly_expenses": "0", "confirmed_monthly_income": "0", "currency": "USD",
            "income_date": "", "pending_debits": "0", "reserved_balance": "0",
            "flexible_monthly_spending": "0", "flexible_reduction": "0",
            "payment_preference": "best_option", "installment_count": "3", "installment_total": ""}
    assert assess_manual(base)["recommended_payment_method"] == "full_payment"
    assert assess_manual(base | {"pending_debits": "6000"})["affordability_status"] == "not_affordable"
    assert assess_manual(base | {"reserved_balance": "6000"})["affordability_status"] == "not_affordable"
    reduced = assess_manual(base | {"flexible_monthly_spending": "6000", "flexible_reduction": "5000"})
    assert reduced["recommended_payment_method"] == "full_payment" and "Reduce flexible spending" in reduced["spending_changes_needed"]
    installment = assess_manual(base | {"payment_preference": "installments", "installment_total": "12000"})
    assert installment["recommended_payment_method"] == "installments" and "4,000" in installment["payment_plan"]
    deadline = assess_manual(base | {"payment_preference": "installments", "installment_total": "12000", "desired_completion_date": date.today().isoformat()})
    assert deadline["recommended_payment_method"] == "not_recommended"
    orchestrated = run_assessment_agent(base, assess_manual)
    assert orchestrated["recommended_payment_method"] == "full_payment"
    assert [step["stage"] for step in orchestrated["agent_trace"]] == ["evidence", "constraints", "plan", "verification"]


def main():
    planner = Planner()
    # Public solved examples are evaluated separately and never used as lookup labels.
    samples = rows("sample_requests.csv")
    generated = [dict(zip(OUT_COLUMNS, planner.decision(r))) for r in samples]
    fields = ["affordability_status", "recommended_payment_method"]
    exact = {field: sum(p[field] == r[field] for p, r in zip(generated, samples)) for field in fields}
    with (ROOT / "output.csv").open(encoding="utf-8", newline="") as f:
        predictions = list(csv.DictReader(f))
    validate(predictions, rows("requests.csv"))
    validate_assessor_controls()
    print(f"validated {len(predictions)} production rows")
    print("public-sample exact matches:", exact)


if __name__ == "__main__":
    main()
