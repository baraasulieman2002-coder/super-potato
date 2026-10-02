-- Namareq financial foundation schema (SQLite).
-- Money columns ending in _minor are integers in the currency's minor unit
-- (fils for JOD).  Dates are ISO-8601 TEXT (YYYY-MM-DD); timestamps are UTC.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Users, permissions, privacy
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN
                    ('owner','manager','accountant','cashier','technician','stockkeeper')),
    preferred_lang  TEXT NOT NULL DEFAULT 'ar' CHECK (preferred_lang IN ('ar','en')),
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Explicit, owner-confirmed grants.  Business access for the accountant is a
-- role default and needs no row; personal access always needs a row.
CREATE TABLE IF NOT EXISTS finance_access_grants (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    profile_kind    TEXT NOT NULL CHECK (profile_kind IN ('business','personal')),
    granted_by      INTEGER NOT NULL REFERENCES users(id),
    confirmed_at    TEXT NOT NULL,
    revoked_at      TEXT,
    revoked_by      INTEGER REFERENCES users(id),
    note            TEXT
);

CREATE TABLE IF NOT EXISTS personal_access_log (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    resource        TEXT NOT NULL,
    action          TEXT NOT NULL,
    allowed         INTEGER NOT NULL,
    at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id),
    profile_id      INTEGER REFERENCES financial_profiles(id),
    table_name      TEXT NOT NULL,
    record_id       INTEGER,
    action          TEXT NOT NULL,
    details_json    TEXT,
    at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------------------------------------------------------------------------
-- Profiles, accounts, categories
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS financial_profiles (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL UNIQUE CHECK (kind IN ('business','personal')),
    name_en         TEXT NOT NULL,
    name_ar         TEXT NOT NULL,
    base_currency   TEXT NOT NULL DEFAULT 'JOD',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS financial_accounts (
    id                      INTEGER PRIMARY KEY,
    profile_id              INTEGER NOT NULL REFERENCES financial_profiles(id),
    account_type            TEXT NOT NULL,
    name_en                 TEXT NOT NULL,
    name_ar                 TEXT NOT NULL,
    currency                TEXT NOT NULL DEFAULT 'JOD',
    opening_balance_minor   INTEGER NOT NULL DEFAULT 0,
    opening_date            TEXT,
    institution             TEXT,
    -- Only a masked reference (e.g. last four digits) is ever stored.
    account_ref_masked      TEXT,
    credit_limit_minor      INTEGER,
    statement_day           INTEGER CHECK (statement_day BETWEEN 1 AND 31),
    payment_due_day         INTEGER CHECK (payment_due_day BETWEEN 1 AND 31),
    interest_rate           TEXT,
    is_active               INTEGER NOT NULL DEFAULT 1,
    system_code             TEXT,
    notes                   TEXT,
    created_by              INTEGER REFERENCES users(id),
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (profile_id, system_code)
);

CREATE TABLE IF NOT EXISTS income_categories (
    id              INTEGER PRIMARY KEY,
    profile_kind    TEXT NOT NULL CHECK (profile_kind IN ('business','personal')),
    code            TEXT NOT NULL,
    name_en         TEXT NOT NULL,
    name_ar         TEXT NOT NULL,
    parent_id       INTEGER REFERENCES income_categories(id),
    -- system_only categories are fed by other modules (e.g. POS sales) and
    -- cannot be entered manually, which prevents duplicated shop sales.
    system_only     INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (profile_kind, code)
);

CREATE TABLE IF NOT EXISTS expense_categories (
    id              INTEGER PRIMARY KEY,
    profile_kind    TEXT NOT NULL CHECK (profile_kind IN ('business','personal')),
    code            TEXT NOT NULL,
    name_en         TEXT NOT NULL,
    name_ar         TEXT NOT NULL,
    parent_id       INTEGER REFERENCES expense_categories(id),
    is_fixed_cost   INTEGER NOT NULL DEFAULT 0,
    is_cost_of_sales INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (profile_kind, code)
);

-- ---------------------------------------------------------------------------
-- Periods and the immutable double-entry ledger
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS financial_periods (
    id              INTEGER PRIMARY KEY,
    profile_id      INTEGER NOT NULL REFERENCES financial_profiles(id),
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    closed_by       INTEGER REFERENCES users(id),
    closed_at       TEXT,
    UNIQUE (profile_id, start_date)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id                  INTEGER PRIMARY KEY,
    profile_id          INTEGER NOT NULL REFERENCES financial_profiles(id),
    entry_date          TEXT NOT NULL,
    entry_type          TEXT NOT NULL,
    currency            TEXT NOT NULL,
    description         TEXT NOT NULL,
    reference           TEXT,
    source_type         TEXT,
    source_id           INTEGER,
    related_entity_type TEXT,
    related_entity_id   INTEGER,
    transfer_id         INTEGER REFERENCES transfers(id),
    reverses_entry_id   INTEGER REFERENCES ledger_entries(id),
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_entries_profile_date ON ledger_entries(profile_id, entry_date);
CREATE UNIQUE INDEX IF NOT EXISTS ux_entries_reversal ON ledger_entries(reverses_entry_id)
    WHERE reverses_entry_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ledger_lines (
    id                  INTEGER PRIMARY KEY,
    entry_id            INTEGER NOT NULL REFERENCES ledger_entries(id),
    account_id          INTEGER REFERENCES financial_accounts(id),
    income_category_id  INTEGER REFERENCES income_categories(id),
    expense_category_id INTEGER REFERENCES expense_categories(id),
    debit_minor         INTEGER NOT NULL DEFAULT 0 CHECK (debit_minor >= 0),
    credit_minor        INTEGER NOT NULL DEFAULT 0 CHECK (credit_minor >= 0),
    memo                TEXT,
    CHECK ((account_id IS NOT NULL) + (income_category_id IS NOT NULL)
           + (expense_category_id IS NOT NULL) = 1),
    CHECK ((debit_minor > 0 AND credit_minor = 0) OR (credit_minor > 0 AND debit_minor = 0))
);
CREATE INDEX IF NOT EXISTS ix_lines_entry ON ledger_lines(entry_id);
CREATE INDEX IF NOT EXISTS ix_lines_account ON ledger_lines(account_id);

-- Posted entries are immutable: corrections use reversal entries.
CREATE TRIGGER IF NOT EXISTS trg_entries_no_update BEFORE UPDATE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'posted ledger entries are immutable; post a reversal'); END;
CREATE TRIGGER IF NOT EXISTS trg_entries_no_delete BEFORE DELETE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'posted ledger entries are immutable; post a reversal'); END;
CREATE TRIGGER IF NOT EXISTS trg_lines_no_update BEFORE UPDATE ON ledger_lines
BEGIN SELECT RAISE(ABORT, 'posted ledger lines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_lines_no_delete BEFORE DELETE ON ledger_lines
BEGIN SELECT RAISE(ABORT, 'posted ledger lines are immutable'); END;

-- Profile separation enforced inside the database, not only in code.
CREATE TRIGGER IF NOT EXISTS trg_lines_account_profile BEFORE INSERT ON ledger_lines
WHEN NEW.account_id IS NOT NULL AND
     (SELECT profile_id FROM financial_accounts WHERE id = NEW.account_id) <>
     (SELECT profile_id FROM ledger_entries WHERE id = NEW.entry_id)
BEGIN SELECT RAISE(ABORT, 'ledger line account belongs to a different financial profile'); END;

CREATE TRIGGER IF NOT EXISTS trg_lines_income_profile BEFORE INSERT ON ledger_lines
WHEN NEW.income_category_id IS NOT NULL AND
     (SELECT profile_kind FROM income_categories WHERE id = NEW.income_category_id) <>
     (SELECT p.kind FROM ledger_entries e JOIN financial_profiles p ON p.id = e.profile_id
       WHERE e.id = NEW.entry_id)
BEGIN SELECT RAISE(ABORT, 'income category belongs to a different financial profile'); END;

CREATE TRIGGER IF NOT EXISTS trg_lines_expense_profile BEFORE INSERT ON ledger_lines
WHEN NEW.expense_category_id IS NOT NULL AND
     (SELECT profile_kind FROM expense_categories WHERE id = NEW.expense_category_id) <>
     (SELECT p.kind FROM ledger_entries e JOIN financial_profiles p ON p.id = e.profile_id
       WHERE e.id = NEW.entry_id)
BEGIN SELECT RAISE(ABORT, 'expense category belongs to a different financial profile'); END;

-- ---------------------------------------------------------------------------
-- Income, expenses, transfers, owner equity
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS income_transactions (
    id                  INTEGER PRIMARY KEY,
    profile_id          INTEGER NOT NULL REFERENCES financial_profiles(id),
    account_id          INTEGER NOT NULL REFERENCES financial_accounts(id),
    category_id         INTEGER NOT NULL REFERENCES income_categories(id),
    txn_date            TEXT NOT NULL,
    entry_type          TEXT NOT NULL DEFAULT 'income',
    source_name         TEXT,
    amount_minor        INTEGER NOT NULL CHECK (amount_minor > 0),
    tax_minor           INTEGER NOT NULL DEFAULT 0 CHECK (tax_minor >= 0),
    currency            TEXT NOT NULL,
    description         TEXT,
    is_recurring        INTEGER NOT NULL DEFAULT 0,
    recurring_id        INTEGER REFERENCES recurring_transactions(id),
    reference           TEXT,
    notes               TEXT,
    attachment_path     TEXT,
    related_entity_type TEXT,
    related_entity_id   INTEGER,
    source_type         TEXT,
    source_id           INTEGER,
    entry_id            INTEGER NOT NULL REFERENCES ledger_entries(id),
    status              TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','reversed')),
    reversal_entry_id   INTEGER REFERENCES ledger_entries(id),
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- A sale/invoice can feed income exactly once.
CREATE UNIQUE INDEX IF NOT EXISTS ux_income_source ON income_transactions(source_type, source_id)
    WHERE source_type IS NOT NULL;

CREATE TABLE IF NOT EXISTS expense_transactions (
    id                  INTEGER PRIMARY KEY,
    profile_id          INTEGER NOT NULL REFERENCES financial_profiles(id),
    account_id          INTEGER NOT NULL REFERENCES financial_accounts(id),
    category_id         INTEGER NOT NULL REFERENCES expense_categories(id),
    subcategory         TEXT,
    txn_date            TEXT NOT NULL,
    entry_type          TEXT NOT NULL DEFAULT 'expense',
    amount_minor        INTEGER NOT NULL CHECK (amount_minor > 0),
    tax_minor           INTEGER NOT NULL DEFAULT 0 CHECK (tax_minor >= 0),
    tax_recoverable     INTEGER NOT NULL DEFAULT 0,
    currency            TEXT NOT NULL,
    payee               TEXT,
    description         TEXT,
    reference           TEXT,
    is_recurring        INTEGER NOT NULL DEFAULT 0,
    recurring_id        INTEGER REFERENCES recurring_transactions(id),
    bill_id             INTEGER REFERENCES bills(id),
    due_date            TEXT,
    paid_status         TEXT NOT NULL DEFAULT 'paid' CHECK (paid_status IN ('paid','unpaid')),
    paid_entry_id       INTEGER REFERENCES ledger_entries(id),
    business_purpose    TEXT,
    approval_status     TEXT NOT NULL DEFAULT 'not_required'
                        CHECK (approval_status IN ('not_required','pending','approved','rejected')),
    attachment_path     TEXT,
    notes               TEXT,
    related_entity_type TEXT,
    related_entity_id   INTEGER,
    entry_id            INTEGER NOT NULL REFERENCES ledger_entries(id),
    status              TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','reversed')),
    reversal_entry_id   INTEGER REFERENCES ledger_entries(id),
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS transfers (
    id                  INTEGER PRIMARY KEY,
    transfer_type       TEXT NOT NULL,
    txn_date            TEXT NOT NULL,
    amount_minor        INTEGER NOT NULL CHECK (amount_minor > 0),
    currency            TEXT NOT NULL,
    business_entry_id   INTEGER REFERENCES ledger_entries(id),
    personal_entry_id   INTEGER REFERENCES ledger_entries(id),
    business_account_id INTEGER REFERENCES financial_accounts(id),
    personal_account_id INTEGER REFERENCES financial_accounts(id),
    from_account_id     INTEGER REFERENCES financial_accounts(id),
    to_account_id       INTEGER REFERENCES financial_accounts(id),
    description         TEXT NOT NULL,
    reference           TEXT,
    details_json        TEXT,
    status              TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','reversed')),
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS owner_equity_transactions (
    id              INTEGER PRIMARY KEY,
    transfer_id     INTEGER NOT NULL REFERENCES transfers(id),
    profile_id      INTEGER NOT NULL REFERENCES financial_profiles(id),
    kind            TEXT NOT NULL CHECK (kind IN
                    ('contribution','additional_investment','withdrawal','salary',
                     'distribution','loan_in','loan_repayment')),
    txn_date        TEXT NOT NULL,
    amount_minor    INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS recurring_transactions (
    id                  INTEGER PRIMARY KEY,
    profile_id          INTEGER NOT NULL REFERENCES financial_profiles(id),
    kind                TEXT NOT NULL CHECK (kind IN ('income','expense','savings','planned_purchase')),
    name                TEXT NOT NULL,
    account_id          INTEGER REFERENCES financial_accounts(id),
    income_category_id  INTEGER REFERENCES income_categories(id),
    expense_category_id INTEGER REFERENCES expense_categories(id),
    amount_minor        INTEGER NOT NULL CHECK (amount_minor > 0),
    currency            TEXT NOT NULL DEFAULT 'JOD',
    frequency           TEXT NOT NULL,
    custom_interval_days INTEGER,
    next_date           TEXT NOT NULL,
    end_date            TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    notes               TEXT,
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------------------------------------------------------------------------
-- Bills
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bills (
    id                  INTEGER PRIMARY KEY,
    profile_id          INTEGER NOT NULL REFERENCES financial_profiles(id),
    payee               TEXT NOT NULL,
    category_id         INTEGER NOT NULL REFERENCES expense_categories(id),
    description         TEXT,
    period_start        TEXT,
    period_end          TEXT,
    issue_date          TEXT,
    due_date            TEXT NOT NULL,
    amount_minor        INTEGER NOT NULL CHECK (amount_minor > 0),
    tax_minor           INTEGER NOT NULL DEFAULT 0 CHECK (tax_minor >= 0),
    currency            TEXT NOT NULL DEFAULT 'JOD',
    frequency           TEXT NOT NULL DEFAULT 'one_time',
    custom_interval_days INTEGER,
    payment_account_id  INTEGER REFERENCES financial_accounts(id),
    autopay             INTEGER NOT NULL DEFAULT 0,
    is_cancelled        INTEGER NOT NULL DEFAULT 0,
    reminder_days       TEXT NOT NULL DEFAULT '7,1,0',
    series_id           INTEGER REFERENCES bills(id),
    next_generated      INTEGER NOT NULL DEFAULT 0,
    attachment_path     TEXT,
    notes               TEXT,
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS bill_payments (
    id                  INTEGER PRIMARY KEY,
    bill_id             INTEGER NOT NULL REFERENCES bills(id),
    pay_date            TEXT NOT NULL,
    amount_minor        INTEGER NOT NULL CHECK (amount_minor > 0),
    account_id          INTEGER NOT NULL REFERENCES financial_accounts(id),
    expense_txn_id      INTEGER NOT NULL REFERENCES expense_transactions(id),
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------------------------------------------------------------------------
-- Loans
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS loans (
    id                      INTEGER PRIMARY KEY,
    profile_id              INTEGER NOT NULL REFERENCES financial_profiles(id),
    role                    TEXT NOT NULL CHECK (role IN ('borrower','lender')),
    borrower                TEXT NOT NULL,
    lender                  TEXT NOT NULL,
    loan_type               TEXT NOT NULL,
    principal_minor         INTEGER NOT NULL CHECK (principal_minor > 0),
    currency                TEXT NOT NULL DEFAULT 'JOD',
    annual_rate             TEXT NOT NULL DEFAULT '0',
    interest_method         TEXT NOT NULL CHECK (interest_method IN ('reducing_balance','flat','none')),
    start_date              TEXT NOT NULL,
    first_payment_date      TEXT NOT NULL,
    maturity_date           TEXT NOT NULL,
    frequency               TEXT NOT NULL,
    term_count              INTEGER NOT NULL CHECK (term_count > 0),
    installment_minor       INTEGER NOT NULL,
    cash_account_id         INTEGER NOT NULL REFERENCES financial_accounts(id),
    loan_account_id         INTEGER NOT NULL REFERENCES financial_accounts(id),
    counterpart_loan_id     INTEGER REFERENCES loans(id),
    collateral              TEXT,
    fees_minor              INTEGER NOT NULL DEFAULT 0,
    early_settlement_fee_pct TEXT NOT NULL DEFAULT '0',
    early_payment_conditions TEXT,
    status                  TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                            ('active','completed','overdue','defaulted','restructured')),
    origination_entry_id    INTEGER REFERENCES ledger_entries(id),
    notes                   TEXT,
    created_by              INTEGER REFERENCES users(id),
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS loan_installments (
    id                  INTEGER PRIMARY KEY,
    loan_id             INTEGER NOT NULL REFERENCES loans(id),
    seq                 INTEGER NOT NULL,
    due_date            TEXT NOT NULL,
    principal_minor     INTEGER NOT NULL,
    interest_minor      INTEGER NOT NULL,
    paid_principal_minor INTEGER NOT NULL DEFAULT 0,
    paid_interest_minor INTEGER NOT NULL DEFAULT 0,
    paid_date           TEXT,
    status              TEXT NOT NULL DEFAULT 'scheduled'
                        CHECK (status IN ('scheduled','partial','paid','waived')),
    UNIQUE (loan_id, seq)
);

CREATE TABLE IF NOT EXISTS loan_payments (
    id                  INTEGER PRIMARY KEY,
    loan_id             INTEGER NOT NULL REFERENCES loans(id),
    pay_date            TEXT NOT NULL,
    principal_minor     INTEGER NOT NULL,
    interest_minor      INTEGER NOT NULL,
    fee_minor           INTEGER NOT NULL DEFAULT 0,
    kind                TEXT NOT NULL CHECK (kind IN ('installment','extra','settlement')),
    entry_id            INTEGER REFERENCES ledger_entries(id),
    transfer_id         INTEGER REFERENCES transfers(id),
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------------------------------------------------------------------------
-- Debts and receivables
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS debt_records (
    id                  INTEGER PRIMARY KEY,
    profile_id          INTEGER NOT NULL REFERENCES financial_profiles(id),
    direction           TEXT NOT NULL CHECK (direction IN ('receivable','payable')),
    debt_type           TEXT NOT NULL,
    debtor              TEXT NOT NULL,
    creditor            TEXT NOT NULL,
    original_minor      INTEGER NOT NULL CHECK (original_minor > 0),
    currency            TEXT NOT NULL DEFAULT 'JOD',
    issue_date          TEXT NOT NULL,
    due_date            TEXT,
    installment_plan_json TEXT,
    annual_rate         TEXT NOT NULL DEFAULT '0',
    account_id          INTEGER NOT NULL REFERENCES financial_accounts(id),
    origin              TEXT NOT NULL,
    reference           TEXT,
    status              TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','settled','written_off')),
    entry_id            INTEGER REFERENCES ledger_entries(id),
    notes               TEXT,
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS debt_payments (
    id                  INTEGER PRIMARY KEY,
    debt_id             INTEGER NOT NULL REFERENCES debt_records(id),
    pay_date            TEXT NOT NULL,
    amount_minor        INTEGER NOT NULL CHECK (amount_minor > 0),
    account_id          INTEGER NOT NULL REFERENCES financial_accounts(id),
    entry_id            INTEGER NOT NULL REFERENCES ledger_entries(id),
    notes               TEXT,
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------------------------------------------------------------------------
-- Assets and liabilities registers
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    id                      INTEGER PRIMARY KEY,
    profile_id              INTEGER NOT NULL REFERENCES financial_profiles(id),
    asset_class             TEXT NOT NULL,
    name_en                 TEXT NOT NULL,
    name_ar                 TEXT NOT NULL,
    purchase_value_minor    INTEGER NOT NULL CHECK (purchase_value_minor >= 0),
    current_value_minor     INTEGER NOT NULL,
    currency                TEXT NOT NULL DEFAULT 'JOD',
    purchase_date           TEXT NOT NULL,
    useful_life_months      INTEGER,
    salvage_value_minor     INTEGER NOT NULL DEFAULT 0,
    depreciation_method     TEXT NOT NULL DEFAULT 'none'
                            CHECK (depreciation_method IN ('none','straight_line','declining_balance')),
    asset_account_id        INTEGER NOT NULL REFERENCES financial_accounts(id),
    accum_dep_account_id    INTEGER REFERENCES financial_accounts(id),
    depreciated_through     TEXT,
    related_loan_id         INTEGER REFERENCES loans(id),
    disposal_date           TEXT,
    sale_proceeds_minor     INTEGER,
    acquisition_entry_id    INTEGER REFERENCES ledger_entries(id),
    notes                   TEXT,
    created_by              INTEGER REFERENCES users(id),
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS liabilities (
    id                  INTEGER PRIMARY KEY,
    profile_id          INTEGER NOT NULL REFERENCES financial_profiles(id),
    liability_class     TEXT NOT NULL,
    name_en             TEXT NOT NULL,
    name_ar             TEXT NOT NULL,
    account_id          INTEGER NOT NULL REFERENCES financial_accounts(id),
    related_loan_id     INTEGER REFERENCES loans(id),
    due_date            TEXT,
    notes               TEXT,
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------------------------------------------------------------------------
-- Budgets and goals
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS budgets (
    id              INTEGER PRIMARY KEY,
    profile_id      INTEGER NOT NULL REFERENCES financial_profiles(id),
    name            TEXT NOT NULL,
    period_type     TEXT NOT NULL CHECK (period_type IN ('monthly','annual')),
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    rollover        INTEGER NOT NULL DEFAULT 0,
    created_by      INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (profile_id, period_type, start_date)
);

CREATE TABLE IF NOT EXISTS budget_lines (
    id                  INTEGER PRIMARY KEY,
    budget_id           INTEGER NOT NULL REFERENCES budgets(id),
    line_type           TEXT NOT NULL CHECK (line_type IN
                        ('income','expense','savings','debt_payment','capital')),
    income_category_id  INTEGER REFERENCES income_categories(id),
    expense_category_id INTEGER REFERENCES expense_categories(id),
    area                TEXT,
    amount_minor        INTEGER NOT NULL CHECK (amount_minor >= 0),
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS financial_goals (
    id                      INTEGER PRIMARY KEY,
    profile_id              INTEGER NOT NULL REFERENCES financial_profiles(id),
    goal_type               TEXT NOT NULL,
    name_en                 TEXT NOT NULL,
    name_ar                 TEXT NOT NULL,
    target_minor            INTEGER NOT NULL CHECK (target_minor > 0),
    starting_minor          INTEGER NOT NULL DEFAULT 0,
    currency                TEXT NOT NULL DEFAULT 'JOD',
    start_date              TEXT NOT NULL,
    target_date             TEXT NOT NULL,
    contribution_frequency  TEXT NOT NULL DEFAULT 'monthly',
    linked_account_id       INTEGER REFERENCES financial_accounts(id),
    priority                INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    status                  TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','achieved','paused','cancelled')),
    notes                   TEXT,
    created_by              INTEGER REFERENCES users(id),
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS goal_contributions (
    id              INTEGER PRIMARY KEY,
    goal_id         INTEGER NOT NULL REFERENCES financial_goals(id),
    contrib_date    TEXT NOT NULL,
    amount_minor    INTEGER NOT NULL,
    transfer_id     INTEGER REFERENCES transfers(id),
    notes           TEXT,
    created_by      INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------------------------------------------------------------------------
-- Forecasts, advice, reconciliation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cash_flow_forecasts (
    id              INTEGER PRIMARY KEY,
    scope           TEXT NOT NULL CHECK (scope IN ('business','personal','combined')),
    as_of           TEXT NOT NULL,
    horizon_days    INTEGER NOT NULL,
    result_json     TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    created_by      INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS financial_advice (
    id              INTEGER PRIMARY KEY,
    scope           TEXT NOT NULL CHECK (scope IN ('business','personal','combined')),
    rule_code       TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    params_json     TEXT NOT NULL,
    evidence_json   TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','dismissed','postponed','completed','superseded')),
    postponed_until TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_advice_fp ON financial_advice(fingerprint);

CREATE TABLE IF NOT EXISTS advice_actions (
    id              INTEGER PRIMARY KEY,
    advice_id       INTEGER NOT NULL REFERENCES financial_advice(id),
    user_id         INTEGER NOT NULL REFERENCES users(id),
    action          TEXT NOT NULL CHECK (action IN ('dismissed','postponed','completed','reopened')),
    note            TEXT,
    at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS account_reconciliations (
    id                      INTEGER PRIMARY KEY,
    account_id              INTEGER NOT NULL REFERENCES financial_accounts(id),
    kind                    TEXT NOT NULL CHECK (kind IN ('bank_statement','cash_count','card_statement')),
    statement_date          TEXT NOT NULL,
    statement_balance_minor INTEGER NOT NULL,
    ledger_balance_minor    INTEGER NOT NULL,
    difference_minor        INTEGER NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('balanced','difference')),
    adjustment_entry_id     INTEGER REFERENCES ledger_entries(id),
    notes                   TEXT,
    reconciled_by           INTEGER REFERENCES users(id),
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
