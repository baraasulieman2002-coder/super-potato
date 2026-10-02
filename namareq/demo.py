"""Realistic demo data for a small computer/phone shop in Amman.

Used by the test suite and by ``python -m namareq demo`` so the interface
can be explored with meaningful numbers.  All names are fictional.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import accounts, assets, bills, db, debts, loans, permissions, planning, transactions, transfers
from .money import to_minor as m


def build(conn, today: str = "2026-10-02", password: str = "owner-pass-123") -> dict:
    db.init_db(conn)
    owner = permissions.get_user(conn, permissions.create_user(conn, "owner", "Rami Haddad", password, "owner"))
    acc_id = permissions.create_user(conn, "accountant", "Lina Accountant", "accountant-pass-1", "accountant", "en")
    cashier_id = permissions.create_user(conn, "cashier", "Omar Cashier", "cashier-pass-12", "cashier")
    t0 = date.fromisoformat(today)
    start = (t0.replace(day=1) - timedelta(days=125)).replace(day=1)
    s = start.isoformat()

    B = dict(profile="business")
    bank = accounts.create_account(conn, owner, **B, account_type="business_bank", name_en="Arab Bank — shop",
                                   name_ar="البنك العربي — المحل", institution="Arab Bank", account_ref="JO71ARAB1180000000118000123456",
                                   opening_balance_minor=m("6500"), opening_date=s)
    register = accounts.create_account(conn, owner, **B, account_type="business_cash", name_en="Shop register",
                                       name_ar="صندوق المحل", opening_balance_minor=m("300"), opening_date=s)
    clearing = accounts.create_account(conn, owner, **B, account_type="card_clearing", name_en="Card terminal clearing",
                                       name_ar="تسويات جهاز البطاقات")
    P = dict(profile="personal")
    pbank = accounts.create_account(conn, owner, **P, account_type="personal_bank", name_en="Housing Bank — personal",
                                    name_ar="بنك الإسكان — شخصي", institution="Housing Bank",
                                    opening_balance_minor=m("2200"), opening_date=s)
    pcash = accounts.create_account(conn, owner, **P, account_type="personal_cash", name_en="Wallet", name_ar="المحفظة",
                                    opening_balance_minor=m("150"), opening_date=s)
    savings = accounts.create_account(conn, owner, **P, account_type="savings", name_en="Savings", name_ar="حساب التوفير",
                                      opening_balance_minor=m("1800"), opening_date=s)
    card = accounts.create_account(conn, owner, **P, account_type="credit_card", name_en="Visa card", name_ar="بطاقة فيزا",
                                   credit_limit_minor=m("2000"), account_ref="4111111111111111", statement_day=25,
                                   payment_due_day=15)

    bc = lambda code: db.expense_category(conn, "business", code)["id"]  # noqa: E731
    pc = lambda code: db.expense_category(conn, "personal", code)["id"]  # noqa: E731
    bi = lambda code: db.income_category(conn, "business", code)["id"]  # noqa: E731

    sale_id = 1000
    d = start
    while d < t0:
        iso = d.isoformat()
        wd = d.weekday()
        if wd != 4:  # closed Fridays
            base = 180 + (d.day % 7) * 15 + (40 if d.month >= 9 else 0)
            transactions.post_shop_sale(conn, owner, sale_id=sale_id, txn_date=iso, account_id=register,
                                        net_minor=m(str(base)), tax_minor=m(str(round(base * 0.16, 3))))
            sale_id += 1
            if d.day % 3 == 0:
                transactions.record_income(conn, owner, **B, account_id=clearing, category_id=bi("repair_services"),
                                           txn_date=iso, amount_minor=m("45"), tax_minor=m("7.2"), source_name="Walk-in repair")
            if d.day % 10 == 0:
                transactions.record_income(conn, owner, **B, account_id=bank, category_id=bi("pc_assembly"),
                                           txn_date=iso, amount_minor=m("120"), tax_minor=m("19.2"), source_name="Gaming PC build")
        if d.day == 2:
            transactions.record_expense(conn, owner, **B, account_id=bank, category_id=bc("inventory_purchases"),
                                        txn_date=iso, amount_minor=m("3200"), tax_minor=m("512"), tax_recoverable=True,
                                        payee="Tech Distribution Co.")
            transactions.record_expense(conn, owner, **B, account_id=bank, category_id=bc("salaries"), txn_date=iso,
                                        amount_minor=m("850"), payee="Staff payroll")
        if d.day == 5:
            transactions.record_expense(conn, owner, **B, account_id=bank, category_id=bc("internet"), txn_date=iso,
                                        amount_minor=m("35"), payee="Orange Jordan")
            transactions.record_expense(conn, owner, **P, account_id=pbank, category_id=pc("housing"), txn_date=iso,
                                        amount_minor=m("350"), payee="Landlord — apartment")
        if d.day == 1 and d > start:
            transfers.owner_transfer(conn, owner, transfer_type="owner_salary", txn_date=iso, amount_minor=m("700"),
                                     description="Monthly owner salary", business_account_id=bank, personal_account_id=pbank)
            transfers.internal_transfer(conn, owner, **P, from_account_id=pbank, to_account_id=savings, txn_date=iso,
                                        amount_minor=m("100"), description="Monthly saving")
        if wd in (3, 5):
            amount = "18" if d.month < 9 else "34"
            transactions.record_expense(conn, owner, **P, account_id=pcash, category_id=pc("dining"), txn_date=iso,
                                        amount_minor=m(amount), payee="Hashem Restaurant")
        if wd == 6:
            transactions.record_expense(conn, owner, **P, account_id=card, category_id=pc("groceries"), txn_date=iso,
                                        amount_minor=m("42"), payee="Carrefour")
        d += timedelta(days=1)

    # Register cash goes to the bank weekly in reality; keep it simple: one sweep per month.
    for months_back in range(4, 0, -1):
        sweep = (t0.replace(day=1) - timedelta(days=30 * months_back)).replace(day=28)
        if sweep.isoformat() >= s:
            from . import ledger
            bal = ledger.account_balance(conn, register, sweep.isoformat())
            if bal > m("400"):
                transfers.internal_transfer(conn, owner, **B, from_account_id=register, to_account_id=bank,
                                            txn_date=sweep.isoformat(), amount_minor=bal - m("300"),
                                            description="Cash deposit to bank")

    # Bills
    bills.create_bill(conn, owner, **B, payee="Landlord — shop", category_id=bc("rent"),
                      due_date=(t0.replace(day=1) - timedelta(days=1)).replace(day=28).isoformat(),
                      amount_minor=m("600"), frequency="monthly", payment_account_id=bank, description="Shop rent")
    bills.create_bill(conn, owner, **B, payee="JEPCO", category_id=bc("electricity"), due_date=(t0 + timedelta(days=6)).isoformat(),
                      amount_minor=m("95"), tax_minor=0, frequency="monthly", payment_account_id=bank, description="Electricity")
    bills.create_bill(conn, owner, **B, payee="Tech Distribution Co.", category_id=bc("inventory_purchases"),
                      due_date=(t0 + timedelta(days=12)).isoformat(), amount_minor=m("9800"),
                      description="Laptop shipment (net 30)")
    bills.create_bill(conn, owner, **P, payee="JEPCO", category_id=pc("utilities"), due_date=(t0 + timedelta(days=4)).isoformat(),
                      amount_minor=m("48"), frequency="monthly", payment_account_id=pbank, description="Home electricity")
    bills.create_bill(conn, owner, **P, payee="Orange Jordan", category_id=pc("subscriptions"),
                      due_date=(t0 - timedelta(days=3)).isoformat(), amount_minor=m("25"), frequency="monthly",
                      autopay=True, payment_account_id=pbank, description="Mobile line")
    bills.generate_recurring_bills(conn, owner, today)

    # Owner / shop interactions
    transfers.owner_transfer(conn, owner, transfer_type="personal_paid_from_business", txn_date=(t0 - timedelta(days=20)).isoformat(),
                             amount_minor=m("52"), description="Home internet paid from shop account",
                             business_account_id=bank, personal_expense_category_id=pc("utilities"))
    transfers.owner_transfer(conn, owner, transfer_type="business_paid_from_personal", txn_date=(t0 - timedelta(days=15)).isoformat(),
                             amount_minor=m("240"), description="Shop shelves paid with personal Visa",
                             personal_account_id=card, business_expense_category_id=bc("equipment"),
                             settle_as="due_to_owner")
    transactions.record_expense(conn, owner, **B, account_id=bank, category_id=bc("internet"),
                                txn_date=(t0 - timedelta(days=8)).isoformat(), amount_minor=m("52"), payee="Orange Jordan")

    # Loans
    biz_loan = loans.create_loan(conn, owner, **B, role="borrower", borrower="Shop", lender="Jordan Kuwait Bank",
                      loan_type="bank_loan", principal_minor=m("12000"), annual_rate="9.5",
                      interest_method="reducing_balance", start_date=s,
                      first_payment_date=(start + timedelta(days=31)).replace(day=10).isoformat(), frequency="monthly",
                      term_count=36, cash_account_id=bank, fees_minor=m("120"), early_settlement_fee_pct="1",
                      collateral="Shop equipment")
    car = loans.create_loan(conn, owner, **P, role="borrower", borrower="Rami", lender="Cairo Amman Bank",
                            loan_type="personal_loan", principal_minor=m("8000"), annual_rate="7",
                            interest_method="flat", start_date=s,
                            first_payment_date=(start + timedelta(days=31)).replace(day=20).isoformat(),
                            frequency="monthly", term_count=48, cash_account_id=pbank)
    for lid, acct in ((biz_loan, bank), (car, pbank)):
        for inst in loans.installments(conn, lid):
            if inst["due_date"] < (t0 - timedelta(days=25)).isoformat():
                loans.record_payment(conn, owner, lid, amount_minor=inst["principal_minor"] + inst["interest_minor"],
                                     pay_date=inst["due_date"], cash_account_id=acct)

    # Customer debts and supplier payable
    for name, amount, days_ago in (("Al-Noor School", "1450", 75), ("Yousef Saleh", "320", 40),
                                   ("Petra Clinic", "880", 20), ("Hala Design Studio", "260", 5)):
        debts.create_debt(conn, owner, debt_type="customer", debtor=name, creditor="Shop", original_minor=m(amount),
                          issue_date=(t0 - timedelta(days=days_ago + 15)).isoformat(),
                          due_date=(t0 - timedelta(days=days_ago)).isoformat(), origin="credit_sale",
                          income_category_id=bi("pc_assembly"))
    debts.create_debt(conn, owner, debt_type="owed_to_owner", debtor="Cousin Fadi", creditor="Rami",
                      original_minor=m("500"), issue_date=(t0 - timedelta(days=60)).isoformat(),
                      due_date=(t0 + timedelta(days=30)).isoformat(), origin="cash_lent", cash_account_id=pbank)

    # Assets
    assets.create_asset(conn, owner, **B, asset_class="computers", name_en="Diagnostic workstation",
                        name_ar="محطة الفحص", purchase_value_minor=m("1800"), purchase_date=s, funding="opening",
                        useful_life_months=36, depreciation_method="straight_line")
    assets.create_asset(conn, owner, **P, asset_class="vehicles", name_en="Family car", name_ar="سيارة العائلة",
                        purchase_value_minor=m("14000"), purchase_date="2023-03-01", funding="opening",
                        current_value_minor=m("11500"))
    assets.create_asset(conn, owner, **P, asset_class="gold", name_en="Gold savings", name_ar="ادخار ذهب",
                        purchase_value_minor=m("2600"), purchase_date="2024-06-01", funding="opening")
    assets.depreciate_all(conn, owner, (t0.replace(day=1) - timedelta(days=1)).isoformat())

    # Budgets and goals
    month_start = t0.replace(day=1).isoformat()
    planning.create_budget(conn, owner, **P, name="Household — this month", period_type="monthly", start_date=month_start,
                           lines=[{"line_type": "expense", "expense_category_id": pc("dining"), "amount_minor": m("120")},
                                  {"line_type": "expense", "expense_category_id": pc("groceries"), "amount_minor": m("220")},
                                  {"line_type": "savings", "amount_minor": m("150")}])
    planning.create_budget(conn, owner, **B, name="Shop operating — this month", period_type="monthly",
                           start_date=month_start,
                           lines=[{"line_type": "expense", "expense_category_id": bc("marketing"), "amount_minor": m("150"),
                                   "area": "Sales"},
                                  {"line_type": "expense", "expense_category_id": bc("inventory_purchases"),
                                   "amount_minor": m("4000"), "area": "Stock"}])
    planning.create_goal(conn, owner, **P, goal_type="emergency_fund", name_en="Emergency fund", name_ar="صندوق الطوارئ",
                         target_minor=m("6000"), start_date=s, target_date=(t0 + timedelta(days=540)).isoformat(),
                         linked_account_id=savings, priority=1)
    planning.create_goal(conn, owner, **B, goal_type="cash_reserve", name_en="Three-month cash reserve",
                         name_ar="احتياطي نقدي لثلاثة أشهر", target_minor=m("8000"), start_date=s,
                         target_date=(t0 + timedelta(days=365)).isoformat(), priority=2)
    bills.create_recurring(conn, owner, **P, kind="income", name="Freelance support contract", amount_minor=m("150"),
                           frequency="monthly", next_date=(t0 + timedelta(days=9)).isoformat(),
                           income_category_id=db.income_category(conn, "personal", "freelance")["id"], account_id=pbank)
    return {"owner": owner, "accountant_id": acc_id, "cashier_id": cashier_id, "bank": bank, "register": register,
            "pbank": pbank, "savings": savings, "card": card}
