# Namareq — financial management and owner advisory

The financial foundation of Namareq: separate **shop** and **owner personal** books, explicit owner ↔ shop
workflows, bills, loans, debts, budgets, goals, assets, cash-flow forecasts, reports and a rule-based
**Owner Financial Advisor**. The interface is fully bilingual: Arabic (RTL, the default) and English (LTR).

> This repository did not yet contain Namareq's authentication, inventory or sales modules, so this
> component includes its own minimal users/roles (needed for the privacy rules) and exposes integration
> hooks for sales (`transactions.post_shop_sale`) and inventory (the `inventory` system account).

## Quick start

```bash
pip install -r requirements.txt
python -m namareq demo  --db demo.sqlite                       # four months of realistic demo data
python -m namareq serve --db demo.sqlite --today 2026-10-02    # http://127.0.0.1:5000
# demo logins: owner / owner-pass-123 · accountant / accountant-pass-1 · cashier / cashier-pass-12

python -m unittest discover -s tests -t tests                  # 82 tests
```

For a real installation: `python -m namareq init --db namareq.sqlite`, then
`python -m namareq create-owner --db namareq.sqlite`. Set `NAMAREQ_SECRET_KEY` and run behind a
production WSGI server with HTTPS (the bundled server is for development only).

## How the books are kept

* **Double entry, two profiles.** Every posting is a balanced ledger entry in exactly one profile
  (`business` or `personal`). SQLite triggers reject any line whose account or category belongs to the
  other profile, so separation is enforced by the database itself, not only by the code.
* **Immutable history.** Posted entries cannot be updated or deleted (triggers). Corrections are reversal
  entries that require a reason; closed periods reject new postings.
* **Money** is stored as integers in minor units (JOD = 3 decimals / fils), using `Decimal`
  half-up rounding. Floats are rejected.
* **Owner ↔ shop movements** use one of 13 explicit transfer types. There is no default, so the user must
  choose. Each type posts a matching entry in both books, linked by a `transfers` row. The full posting map
  is in `namareq/transfers.py`. Examples:
  * the shop pays the owner's home electricity → *owner withdrawal* (equity) in the shop, utilities
    expense in personal; shop profit is unchanged, and the shop ledger records a neutral description;
  * the owner pays a supplier from a personal card → shop expense plus *due to owner*, and *due from
    the shop* on the personal side;
  * personal money put into the shop → *capital contribution* or *owner loan*, never revenue.
* **Shop sales** reach income only through `post_shop_sale` (one posting per sale, enforced by a unique
  index). The *product sales* category cannot be entered manually.
* Inter-profile balances mirror each other, so the combined net worth adds the two books without double
  counting. The combined view never merges income or expenses.

## Module map (spec section → code)

| Area | Module |
|---|---|
| Profiles, accounts, reconciliation | `accounts.py`, `chart.py`, `schema.sql` |
| Income and expenses | `transactions.py` |
| Transfers and owner equity | `transfers.py` |
| Bills and recurring items | `bills.py`, `dates.py` |
| Loans and repayment schedules | `loan_math.py` (pure, tested), `loans.py` |
| Debts, receivables and aging | `debts.py` |
| Assets, depreciation, liabilities | `assets.py` |
| Budgets and goals | `planning.py` |
| Cash-flow forecast | `forecast.py` |
| Reports | `reports.py` |
| Health metrics | `metrics.py` |
| Advisor | `advisor.py` |
| Privacy, permissions, access log | `permissions.py` |
| Separate backups / encrypted export | `backup.py` |
| Arabic/English catalogue (1,024 keys) | `i18n/catalog_*.py` |
| Web UI (RTL/LTR) | `web/app.py`, `web/templates`, `web/static/style.css` |

## Assumptions (also shown in the interface)

* **Loans.** *Reducing balance*: the nominal annual rate is compounded per payment period, giving
  installment `A = P·r / (1 − (1+r)^−n)`, rounded half-up to the fils; the last installment absorbs rounding.
  *Flat*: interest = principal × rate × years on the original principal (its effective rate is reported).
  Payments settle the oldest due installment first, interest before principal. Any surplus is an extra
  principal payment, which shortens a reducing-balance loan. Early settlement = principal + interest accrued
  since the last due date (actual/365) + the loan's fee %. The tests check 10,000 at 12% over 12 months =
  888.488 JOD.
* **Bills** are commitments. Each (partial) payment posts an expense on the payment date. *Autopay* is only
  an indicator: nothing is ever paid or posted automatically, and recurring items wait for confirmation.
* **Forecast** (estimate): starts from cash, bank and card-clearing balances; adds dated bills (including
  future repeats), recurring items, loan installments and debts. Other activity is projected at the
  90-day daily average of non-recurring, non-bill, non-transfer transactions. Overdue receivables are
  excluded, and unpaid sales tax is assumed payable in 30 days.
* **Tax.** Business income records general sales tax collected. Business expenses may mark input tax as
  recoverable; personal expenses never can. The tax summary is decision support only, and the UI advises
  review by a qualified Jordanian accountant. Owner-salary deductibility is flagged for the accountant.
* **Valuation.** Shop assets: cost less depreciation (straight line or double-declining, full-month
  convention). Personal assets: the owner's estimate, with revaluations posted to a personal reserve
  rather than to income.
* **Health scores** are weighted rules of thumb with visible scales and weights. Components without data
  are left out and the remaining weights re-normalised; the UI says so.
* **Loan and savings payments** are balance-sheet movements, not expense categories. Budgets track them as
  *debt payment* and *savings* lines.

## Advisor safeguards

The advisor is rule-based (15 rules) and read-only. A test proves that generating advice changes no
financial table. Every recommendation stores and shows six things: why it was generated, the supporting
numbers, the date range, the assumption, the potential benefit and the risk. The owner can dismiss,
postpone or complete an item; decisions are kept in `advice_actions`, and dismissed items are not
re-raised. No AI model or external service is used, and no data leaves the machine. Rules needing data
Namareq doesn't hold yet (slow-moving stock) stay silent instead of guessing.

## Privacy

* Personal finance is visible only to the owner. Managers, cashiers, technicians and stockkeepers can
  never be granted it.
* The accountant sees shop finance by default. Personal access needs an explicit grant, confirmed with
  the owner's password, and can be revoked.
* Every personal-finance access (allowed or denied) is logged and shown to the owner.
* The shared landing page never shows personal figures. Every page carries a coloured banner: 🏪 shop,
  👤 personal (marked private), or ⚖️ combined.
* Business backups contain no personal rows, categories or descriptions (tested). Personal backups and
  exports are owner-only, require the password again, and are encrypted (PBKDF2-SHA256 + Fernet).
* Only the last four characters of account and card numbers are stored. There are no fields for
  passwords, PINs or card security codes.

## Bilingual interface

Every string comes from one paired catalogue (`key → (English, Arabic)`), so the two languages cannot
drift apart. The tests check that every key used in code and templates exists, that placeholders match,
that every Arabic entry contains Arabic text, and that all 65 pages and reports render in both languages with the
right `dir`. The layout uses CSS logical properties only, so RTL mirrors navigation, tables, forms,
badges and print layouts. Charts run right-to-left in Arabic. Switching language returns to the same
page and restores unsaved form input (passwords excluded). Account, category, asset and goal names are
stored in both languages.

## Not done yet / limitations

* **Integration:** Namareq's own auth, inventory and POS modules do not exist here yet. When they do,
  sales should call `post_shop_sale`, and inventory should post to the `inventory` account. Item-level
  inventory valuation and slow-moving-stock analysis depend on that.
* **PDF:** reports have print layouts ("Print / save as PDF" via the browser, RTL preserved). There is
  no server-side PDF generator. Invoices and receipts belong to the sales module.
* **Attachments:** the tables have `attachment_path` columns, but there is no upload UI yet.
* **Multi-currency:** accounts may use other currencies, but an entry must be in a single currency.
  There is no FX conversion; reports assume JOD.
* **Editing:** bills, loans and goals can be created, paid, cancelled or re-statused but have no
  general edit form yet. Posted money records are corrected by reversal, by design.
* The Arabic was written directly, not machine-translated, using standard Jordanian accounting
  terminology. A native-speaker review of tone before release is still recommended.
