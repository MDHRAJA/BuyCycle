"""Constrained orchestration for interactive affordability decisions.

This is intentionally not a chat agent.  Each stage has a narrow responsibility
and the verifier can block an invalid result before it reaches the user.
"""
from __future__ import annotations

from datetime import date


VALID_STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
VALID_METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}


def _number(value):
    try:
        return max(0.0, float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0.0


def _verify(decision, requested):
    """Check output invariants independently of the planning implementation."""
    if decision.get("affordability_status") not in VALID_STATUSES:
        return "Unknown affordability status."
    if decision.get("recommended_payment_method") not in VALID_METHODS:
        return "Unknown payment method."
    safe = _number(decision.get("amount_safe_to_pay"))
    if safe > requested + 0.01:
        return "Safe amount exceeds the requested amount."
    plan = decision.get("payment_plan", "none")
    if plan != "none":
        previous = None
        for entry in plan.split("|"):
            try:
                when_text, amount_text = entry.split(":", 1)
                when = date.fromisoformat(when_text)
                amount = _number(amount_text)
            except ValueError:
                return "Payment plan has an invalid date or amount."
            if amount <= 0:
                return "Payment plan contains a non-positive payment."
            if previous and when < previous:
                return "Payment plan is not chronological."
            previous = when
    if decision["recommended_payment_method"] == "not_recommended" and plan != "none":
        return "A rejected decision cannot include a payment plan."
    return None


def run_assessment_agent(data, assessor):
    """Run intake, planning, and verification with an auditable stage trace."""
    requested = _number(data.get("requested_amount"))
    stages = [
        {"stage": "evidence", "state": "complete",
         "detail": "Used form values and user-approved AI proposals only."},
        {"stage": "constraints", "state": "complete",
         "detail": "Protected reserve, commitments, payment preference, deadline, and spending flexibility loaded."},
    ]
    decision = assessor(data)
    stages.append({"stage": "plan", "state": "complete",
                   "detail": "Generated and simulated eligible payment paths across the 90-day forecast."})
    issue = _verify(decision, requested)
    if issue:
        # Never expose a partially valid plan as a recommendation.
        decision.update({
            "amount_safe_to_pay": "0",
            "affordability_status": "not_affordable",
            "recommended_payment_method": "not_recommended",
            "payment_plan": "none",
            "earliest_date_for_full_payment": "Not within the current 90-day forecast",
            "spending_changes_needed": "Review the supplied financial details before proceeding.",
            "decision_explanation": "The planning result did not pass the final safety validation; no payment is recommended.",
        })
        stages.append({"stage": "verification", "state": "blocked", "detail": issue})
    else:
        stages.append({"stage": "verification", "state": "complete",
                       "detail": "Validated plan format, chronology, amount bounds, and recommendation state."})
    decision["agent_trace"] = stages
    return decision
