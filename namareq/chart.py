"""Chart-of-accounts metadata and bilingual seed data.

Every account type has an accounting class that decides its normal balance
and where it appears on the balance sheet.  Some types are restricted to a
single profile (business cash can never be a personal account, etc.).
"""
from __future__ import annotations

BUSINESS = "business"
PERSONAL = "personal"
BOTH = (BUSINESS, PERSONAL)

# type -> (class, allowed profiles, liquid?)
# class is one of asset, contra_asset, liability, equity.
ACCOUNT_TYPES: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "business_cash": ("asset", (BUSINESS,), True),
    "personal_cash": ("asset", (PERSONAL,), True),
    "business_bank": ("asset", (BUSINESS,), True),
    "personal_bank": ("asset", (PERSONAL,), True),
    "card_clearing": ("asset", BOTH, True),
    "credit_card": ("liability", BOTH, False),
    "savings": ("asset", BOTH, False),
    "investment": ("asset", BOTH, False),
    "customer_receivable": ("asset", (BUSINESS,), False),
    "supplier_payable": ("liability", (BUSINESS,), False),
    "personal_receivable": ("asset", (PERSONAL,), False),
    "personal_payable": ("liability", (PERSONAL,), False),
    "loan_payable": ("liability", BOTH, False),
    "loan_receivable": ("asset", BOTH, False),
    "tax_payable": ("liability", BOTH, False),
    "salary_payable": ("liability", (BUSINESS,), False),
    "owner_equity": ("equity", BOTH, False),
    "fixed_asset": ("asset", BOTH, False),
    "accumulated_depreciation": ("contra_asset", BOTH, False),
    "other_asset": ("asset", BOTH, False),
    "other_liability": ("liability", BOTH, False),
}

# Account types the user may pick when creating an account in the UI.
CASH_LIKE = ("business_cash", "personal_cash", "business_bank", "personal_bank",
             "card_clearing", "savings")
DEBT_PAYMENT_TYPES = ("loan_payable", "credit_card", "personal_payable", "supplier_payable")


def account_class(account_type: str) -> str:
    return ACCOUNT_TYPES[account_type][0]


def is_debit_normal(account_type: str) -> bool:
    return account_class(account_type) == "asset"


def is_liquid(account_type: str) -> bool:
    return ACCOUNT_TYPES[account_type][2]


def allowed_types(profile_kind: str) -> list[str]:
    return [t for t, (_c, profiles, _l) in ACCOUNT_TYPES.items() if profile_kind in profiles]


# System accounts created for every installation.  Cross-profile workflows
# post against these so that both sides always balance.
SYSTEM_ACCOUNTS = {
    BUSINESS: [
        ("opening_equity", "owner_equity", "Opening balance equity", "حقوق ملكية الأرصدة الافتتاحية"),
        ("owner_contributions", "owner_equity", "Owner capital contributions", "مساهمات المالك في رأس المال"),
        ("owner_withdrawals", "owner_equity", "Owner withdrawals", "مسحوبات المالك"),
        ("profit_distributions", "owner_equity", "Profit distributions to owner", "توزيعات الأرباح على المالك"),
        ("revaluation", "owner_equity", "Revaluation reserve", "احتياطي إعادة التقييم"),
        ("due_to_owner", "other_liability", "Due to owner (expenses paid by owner)", "مستحق للمالك (مصاريف دفعها المالك)"),
        ("loan_from_owner", "loan_payable", "Loan payable to owner", "قرض مستحق للمالك"),
        ("due_from_owner", "other_asset", "Due from owner", "مستحق على المالك"),
        ("customer_receivables", "customer_receivable", "Customer receivables", "ذمم العملاء المدينة"),
        ("supplier_payables", "supplier_payable", "Supplier payables", "ذمم الموردين الدائنة"),
        ("sales_tax_payable", "tax_payable", "General sales tax payable", "ضريبة المبيعات العامة المستحقة"),
        ("input_tax", "other_asset", "Recoverable input sales tax", "ضريبة المدخلات القابلة للاسترداد"),
        ("salary_payable", "salary_payable", "Salaries payable", "رواتب مستحقة الدفع"),
        ("inventory", "other_asset", "Inventory", "المخزون"),
        ("employee_advances", "other_asset", "Employee advances", "سلف الموظفين"),
        ("loan_to_owner", "loan_receivable", "Loan receivable from owner", "قرض مستحق على المالك"),
        ("other_receivables", "other_asset", "Other receivables", "ذمم مدينة أخرى"),
        ("other_payables", "other_liability", "Other payables", "ذمم دائنة أخرى"),
    ],
    PERSONAL: [
        ("opening_equity", "owner_equity", "Opening net worth", "صافي الثروة الافتتاحي"),
        ("capital_in_shop", "owner_equity", "Capital invested in the shop", "رأس المال المستثمر في المحل"),
        ("revaluation", "owner_equity", "Revaluation of personal assets", "إعادة تقييم الأصول الشخصية"),
        ("due_from_business", "personal_receivable", "Due from the shop", "مستحق لي على المحل"),
        ("loan_to_shop", "loan_receivable", "Loan to the shop", "قرض مقدَّم للمحل"),
        ("due_to_business", "personal_payable", "Due to the shop", "مستحق علي للمحل"),
        ("loan_from_shop", "loan_payable", "Loan from the shop", "قرض من المحل"),
        ("personal_receivables", "personal_receivable", "Money owed to me", "مبالغ مستحقة لي"),
        ("personal_payables", "personal_payable", "Money I owe", "مبالغ مستحقة علي"),
    ],
}

# code, en, ar, system_only
INCOME_CATEGORIES = {
    BUSINESS: [
        ("product_sales", "Product sales", "مبيعات المنتجات", 1),
        ("repair_services", "Repair services", "خدمات الصيانة", 0),
        ("pc_assembly", "PC assembly", "تجميع الحواسيب", 0),
        ("installation", "Installation services", "خدمات التركيب", 0),
        ("delivery_fees", "Delivery fees", "رسوم التوصيل", 0),
        ("warranty_income", "Warranty income", "إيرادات الكفالة", 0),
        ("commissions", "Commissions", "العمولات", 0),
        ("interest_income", "Interest income", "إيرادات الفوائد", 0),
        ("other_business_income", "Other business income", "إيرادات تجارية أخرى", 0),
    ],
    PERSONAL: [
        ("salary", "Salary", "الراتب", 0),
        ("owner_draws", "Withdrawals from the shop", "مسحوبات من المحل", 1),
        ("owner_distribution", "Business profit distribution", "توزيعات أرباح المحل", 1),
        ("freelance", "Freelance income", "دخل العمل الحر", 0),
        ("rental_income", "Rental income", "دخل الإيجارات", 0),
        ("investment_income", "Investment income", "عوائد الاستثمار", 0),
        ("interest_income", "Interest received", "فوائد مقبوضة", 0),
        ("gifts", "Gifts", "هدايا", 0),
        ("refunds", "Refunds", "مبالغ مستردة", 0),
        ("other_personal_income", "Other personal income", "دخل شخصي آخر", 0),
    ],
}

# code, en, ar, is_fixed_cost, is_cost_of_sales
EXPENSE_CATEGORIES = {
    BUSINESS: [
        ("inventory_purchases", "Inventory purchases (cost of goods)", "مشتريات البضاعة (تكلفة المبيعات)", 0, 1),
        ("rent", "Rent", "الإيجار", 1, 0),
        ("salaries", "Employee salaries", "رواتب الموظفين", 1, 0),
        ("owner_salary", "Owner salary", "راتب المالك", 1, 0),
        ("electricity", "Electricity", "الكهرباء", 1, 0),
        ("internet", "Internet and telecom", "الإنترنت والاتصالات", 1, 0),
        ("transportation", "Transportation", "المواصلات", 0, 0),
        ("delivery", "Delivery", "التوصيل", 0, 0),
        ("marketing", "Marketing", "التسويق", 0, 0),
        ("software", "Software and subscriptions", "البرمجيات والاشتراكات", 1, 0),
        ("equipment", "Small equipment", "معدات صغيرة", 0, 0),
        ("repairs_maintenance", "Repairs and maintenance", "الصيانة والإصلاحات", 0, 0),
        ("bank_charges", "Banking charges", "عمولات ومصاريف بنكية", 0, 0),
        ("professional_fees", "Professional fees", "أتعاب مهنية", 0, 0),
        ("government_fees", "Government fees and licences", "رسوم حكومية ورخص", 0, 0),
        ("taxes", "Taxes", "الضرائب", 0, 0),
        ("insurance", "Insurance", "التأمين", 1, 0),
        ("interest_expense", "Loan interest", "فوائد القروض", 0, 0),
        ("depreciation", "Depreciation", "مصروف الاستهلاك", 0, 0),
        ("cash_over_short", "Cash over/short", "فروقات الصندوق", 0, 0),
        ("bad_debts", "Bad debts written off", "ديون معدومة", 0, 0),
        ("misc_business", "Miscellaneous business expenses", "مصاريف تجارية متنوعة", 0, 0),
    ],
    PERSONAL: [
        ("housing", "Housing (rent or mortgage)", "السكن (إيجار أو قسط سكن)", 1, 0),
        ("utilities", "Utilities", "الخدمات (كهرباء وماء)", 1, 0),
        ("groceries", "Groceries", "البقالة والمواد الغذائية", 0, 0),
        ("transportation", "Transportation", "المواصلات", 0, 0),
        ("fuel", "Fuel", "الوقود", 0, 0),
        ("education", "Education", "التعليم", 1, 0),
        ("healthcare", "Healthcare", "الرعاية الصحية", 0, 0),
        ("insurance", "Insurance", "التأمين", 1, 0),
        ("family_support", "Family support", "إعالة الأسرة", 0, 0),
        ("entertainment", "Entertainment", "الترفيه", 0, 0),
        ("dining", "Dining out", "المطاعم", 0, 0),
        ("shopping", "Shopping", "التسوق", 0, 0),
        ("travel", "Travel", "السفر", 0, 0),
        ("subscriptions", "Subscriptions", "الاشتراكات", 1, 0),
        ("charity", "Charity", "الصدقات والتبرعات", 0, 0),
        ("personal_taxes", "Personal taxes", "الضرائب الشخصية", 0, 0),
        ("interest_expense", "Loan interest", "فوائد القروض", 0, 0),
        ("bank_charges", "Bank charges", "عمولات بنكية", 0, 0),
        ("bad_debts", "Money owed to me written off", "ديون شخصية معدومة", 0, 0),
        ("misc_personal", "Miscellaneous personal expenses", "مصاريف شخصية متنوعة", 0, 0),
    ],
}
# Loan principal payments and savings contributions are deliberately NOT
# expense categories: they are balance-sheet movements (a liability goes down,
# or money moves to a savings account).  Budgets track them as their own
# line types ("debt_payment" and "savings") using account movements.

PROFILES = {
    BUSINESS: ("Shop finances", "مالية المحل"),
    PERSONAL: ("Owner personal finances", "المالية الشخصية للمالك"),
}
