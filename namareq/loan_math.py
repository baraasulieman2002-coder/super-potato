"""Loan repayment calculations (pure functions, fully tested).

Assumptions — shown to the user next to every schedule:

1. ``reducing_balance``: the quoted annual rate is a nominal rate compounded
   once per payment period.  Periodic rate r = annual% / 100 / periods per
   year.  The level installment is the standard annuity formula
       A = P * r / (1 - (1 + r) ** -n)
   rounded half-up to the currency minor unit (fils).  Each period's interest
   is balance * r, rounded half-up; principal = A - interest.  The final
   installment absorbs all rounding so the balance ends at exactly zero.
2. ``flat``: total interest = P * annual% / 100 * (n / periods per year),
   charged on the ORIGINAL principal for the whole term (common for Jordanian
   consumer installment loans).  Interest and principal are spread evenly
   across installments, with rounding remainders allocated so the totals are
   exact.  The effective rate of a flat loan is much higher than its quoted
   rate; ``effective_annual_rate`` reports it.
3. ``none``: interest-free (family loans, employee advances); principal is
   spread evenly.
4. Extra payments go entirely to principal.  For reducing-balance loans the
   installment stays the same and the term shortens.  For flat loans the
   lender's early-payment policy decides any interest rebate, so the what-if
   calculator does not assume one.
5. Early settlement = remaining principal + interest accrued since the last
   due date (actual days / 365) + the loan's early-settlement fee percentage
   of the remaining principal.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, getcontext

from . import dates
from .money import round_minor, split_proportionally

getcontext().prec = 34


@dataclass
class ScheduleRow:
    seq: int
    due_date: str
    opening_minor: int
    principal_minor: int
    interest_minor: int
    closing_minor: int

    @property
    def payment_minor(self) -> int:
        return self.principal_minor + self.interest_minor


def periodic_rate(annual_rate, frequency: str) -> Decimal:
    return Decimal(str(annual_rate)) / Decimal(100) / Decimal(dates.PERIODS_PER_YEAR[frequency])


def annuity_payment(principal_minor: int, rate: Decimal, n: int) -> int:
    if n <= 0:
        raise ValueError("term must be positive")
    if rate == 0:
        return round_minor(Decimal(principal_minor) / n)
    factor = (1 + rate) ** n
    return round_minor(Decimal(principal_minor) * rate * factor / (factor - 1))


def build_schedule(principal_minor: int, annual_rate, n: int, frequency: str, method: str,
                   first_payment_date: str, installment_minor: int | None = None,
                   start_seq: int = 1) -> list[ScheduleRow]:
    if principal_minor <= 0 or n <= 0:
        raise ValueError("principal and term must be positive")
    anchor = dates.d(first_payment_date).day

    def due(i: int) -> str:
        return dates.advance(first_payment_date, frequency, i, anchor_day=anchor).isoformat()

    rows: list[ScheduleRow] = []
    if method == "reducing_balance":
        r = periodic_rate(annual_rate, frequency)
        payment = installment_minor or annuity_payment(principal_minor, r, n)
        balance = principal_minor
        i = 0
        while balance > 0:
            interest = round_minor(Decimal(balance) * r)
            last = (i == n - 1 and installment_minor is None) or payment - interest >= balance
            principal = balance if last else payment - interest
            if principal <= 0 and not last:
                raise ValueError("installment does not cover interest; loan never amortizes")
            rows.append(ScheduleRow(start_seq + i, due(i), balance, principal, interest,
                                    balance - principal))
            balance -= principal
            i += 1
            if i > 2000:
                raise ValueError("schedule too long")
        return rows
    if method == "flat":
        years = Decimal(n) / Decimal(dates.PERIODS_PER_YEAR[frequency])
        total_interest = round_minor(Decimal(principal_minor) * Decimal(str(annual_rate)) / 100 * years)
    elif method == "none":
        total_interest = 0
    else:
        raise ValueError(f"unknown interest method {method}")
    principals = split_proportionally(principal_minor, [1] * n)
    interests = split_proportionally(total_interest, [1] * n)
    balance = principal_minor
    for i in range(n):
        rows.append(ScheduleRow(start_seq + i, due(i), balance, principals[i], interests[i],
                                balance - principals[i]))
        balance -= principals[i]
    return rows


def totals(schedule: list[ScheduleRow]) -> dict:
    return {
        "payments": len(schedule),
        "total_principal_minor": sum(r.principal_minor for r in schedule),
        "total_interest_minor": sum(r.interest_minor for r in schedule),
        "total_paid_minor": sum(r.payment_minor for r in schedule),
    }


def effective_annual_rate(principal_minor: int, payment_minor: int, n: int, frequency: str) -> Decimal:
    """Solve for the periodic rate that makes a level ``payment`` repay
    ``principal`` over ``n`` periods (bisection), annualised as nominal %."""
    lo, hi = Decimal(0), Decimal(1)
    if payment_minor * n <= principal_minor:
        return Decimal(0)
    for _ in range(200):
        mid = (lo + hi) / 2
        if annuity_payment(principal_minor, mid, n) > payment_minor:
            hi = mid
        else:
            lo = mid
    return ((lo + hi) / 2 * dates.PERIODS_PER_YEAR[frequency] * 100).quantize(Decimal("0.01"))


def simulate_extra_payment(balance_minor: int, annual_rate, frequency: str, payment_minor: int,
                           extra_minor: int) -> dict:
    """What-if: pay ``extra`` on top of the regular installment every period
    on a reducing-balance loan.  Returns periods and interest with and
    without the extra, so the saving is traceable."""
    r = periodic_rate(annual_rate, frequency)

    def run(extra: int) -> tuple[int, int]:
        balance, periods, interest_total = balance_minor, 0, 0
        while balance > 0:
            interest = round_minor(Decimal(balance) * r)
            principal = min(balance, payment_minor + extra - interest)
            if principal <= 0:
                raise ValueError("installment does not cover interest")
            interest_total += interest
            balance -= principal
            periods += 1
            if periods > 5000:
                raise ValueError("schedule too long")
        return periods, interest_total

    base_periods, base_interest = run(0)
    new_periods, new_interest = run(extra_minor)
    return {
        "base_periods": base_periods, "base_interest_minor": base_interest,
        "new_periods": new_periods, "new_interest_minor": new_interest,
        "interest_saved_minor": base_interest - new_interest,
        "periods_saved": base_periods - new_periods,
    }


def early_settlement_amount(balance_minor: int, annual_rate, days_since_last_due: int,
                            fee_pct, method: str) -> dict:
    accrued = 0
    if method == "reducing_balance" and days_since_last_due > 0:
        accrued = round_minor(Decimal(balance_minor) * Decimal(str(annual_rate)) / 100
                              * Decimal(days_since_last_due) / 365)
    fee = round_minor(Decimal(balance_minor) * Decimal(str(fee_pct)) / 100)
    return {"principal_minor": balance_minor, "accrued_interest_minor": accrued, "fee_minor": fee,
            "total_minor": balance_minor + accrued + fee}
