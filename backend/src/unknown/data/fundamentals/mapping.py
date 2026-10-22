"""Versioned mapping from XBRL concepts to standardized line items (CAP-CO-05; book ch. 11).

This is the project's own documented rule set (vendor standardization rules are proprietary and
are not copied, CAP-CO-05 ``unavailable_or_restricted``). Rule identity is ``std-map@<version>``;
every standardized value names the rule, the concept that supplied it and the observation ids.

Precedence. ``concepts`` lists *semantically equivalent* alternates in precedence order. For each
period the first concept with a reported fact for exactly that period wins, so a filer that moved
from ``SalesRevenueNet`` to ``RevenueFromContractWithCustomerExcludingAssessedTax`` (ASC 606) keeps
one continuous revenue line; the concept used is shown per cell. Concepts that are only partial
(e.g. goods-only revenue) are deliberately *not* alternates.

Computed items. ``compute`` lists signed terms over other line items. ``compute_when='fallback'``
computes only when no concept is reported for that period (e.g. gross profit = revenue - cost of
revenue); ``'always'`` is a pure derivation (EBITDA, free cash flow, total debt). Terms marked
optional may be absent: the cell is still computed, and each absent optional term is listed in the
cell's ``not_reported`` field (never silently treated as a reported zero).

Signs and units follow XBRL as reported: expenses and payments are positive numbers (capital
expenditure ``PaymentsToAcquirePropertyPlantAndEquipment`` is a positive outflow, so
FCF = CFO - capex). Money items use the filer's reporting currency unit; per-share items
``<ccy>/shares``; share counts ``shares``. Per-share values and share counts are never summed
or differenced (no derived quarters, no TTM sums).

Scope of v1: commercial and industrial filers under us-gaap, with ifrs-full alternates for the
main totals (20-F filers). Banks and insurers have different statement structures (CAP-CO-05 open
question); their EV-based multiples and DCF template are suppressed elsewhere by SIC.
"""
from __future__ import annotations

from dataclasses import dataclass

MAPPING_ID = "std-map"
MAPPING_VERSION = 1
MAPPING_REF = f"{MAPPING_ID}@{MAPPING_VERSION}"

STATEMENTS = ("income", "balance", "cash_flow", "per_share", "shares")


@dataclass(frozen=True)
class Term:
    sign: int            # +1 or -1
    item: str            # line-item key
    required: bool = True


@dataclass(frozen=True)
class LineItem:
    key: str
    label: str
    statement: str               # one of STATEMENTS
    period_type: str             # "duration" | "instant"
    unit_kind: str               # "money" | "per_share" | "shares"
    concepts: tuple[str, ...] = ()
    compute: tuple[Term, ...] = ()
    compute_when: str = "fallback"   # "fallback" | "always"
    note: str = ""

    @property
    def summable(self) -> bool:
        """Only money flows add across periods; balances, per-share values and share counts do not."""
        return self.unit_kind == "money" and self.period_type == "duration"

    def rule(self) -> str:
        return f"{MAPPING_REF}:{self.key}"


def _g(*names: str) -> tuple[str, ...]:
    return tuple(n if ":" in n else f"us-gaap:{n}" for n in names)


D, I = "duration", "instant"
M, PS, SH = "money", "per_share", "shares"

LINE_ITEMS: tuple[LineItem, ...] = (
    # ---------------------------------------------------------------- income statement
    LineItem("revenue", "Revenue", "income", D, M, _g(
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax", "ifrs-full:Revenue"),
        note="Total revenue. Goods-only or services-only concepts are not alternates."),
    LineItem("cost_of_revenue", "Cost of revenue", "income", D, M, _g(
        "CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold", "ifrs-full:CostOfSales")),
    LineItem("gross_profit", "Gross profit", "income", D, M, _g("GrossProfit", "ifrs-full:GrossProfit"),
             compute=(Term(1, "revenue"), Term(-1, "cost_of_revenue")), compute_when="fallback",
             note="Reported GrossProfit; otherwise revenue - cost of revenue (computed, flagged)."),
    LineItem("research_and_development", "Research and development", "income", D, M, _g(
        "ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost")),
    LineItem("selling_general_admin", "Selling, general and administrative", "income", D, M, _g(
        "SellingGeneralAndAdministrativeExpense")),
    LineItem("operating_expenses", "Operating expenses", "income", D, M, _g("OperatingExpenses")),
    LineItem("operating_income", "Operating income (loss)", "income", D, M, _g(
        "OperatingIncomeLoss", "ifrs-full:ProfitLossFromOperatingActivities")),
    LineItem("ebit", "EBIT (operating income)", "income", D, M, (),
             compute=(Term(1, "operating_income"),), compute_when="always",
             note="Convention: EBIT = operating income as reported; non-operating items excluded."),
    LineItem("interest_expense", "Interest expense", "income", D, M, _g(
        "InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt", "ifrs-full:FinanceCosts")),
    LineItem("pretax_income", "Income before income taxes", "income", D, M, _g(
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "ifrs-full:ProfitLossBeforeTax")),
    LineItem("income_tax", "Income tax expense (benefit)", "income", D, M, _g(
        "IncomeTaxExpenseBenefit", "ifrs-full:IncomeTaxExpenseContinuingOperations")),
    LineItem("net_income", "Net income attributable to parent", "income", D, M, _g(
        "NetIncomeLoss", "ifrs-full:ProfitLossAttributableToOwnersOfParent"),
        note="Attributable to the parent's shareholders; ProfitLoss (incl. NCI) is a different item."),
    LineItem("net_income_including_nci", "Net income including noncontrolling interest", "income", D, M, _g(
        "ProfitLoss", "ifrs-full:ProfitLoss")),
    LineItem("ebitda", "EBITDA (operating income + D&A)", "income", D, M, (),
             compute=(Term(1, "operating_income"), Term(1, "depreciation_amortization")), compute_when="always",
             note="Convention: EBITDA = operating income + depreciation, depletion and amortization from the cash-flow statement."),
    # ---------------------------------------------------------------- per share and shares
    LineItem("eps_basic", "EPS, basic", "per_share", D, PS, _g("EarningsPerShareBasic", "ifrs-full:BasicEarningsLossPerShare")),
    LineItem("eps_diluted", "EPS, diluted", "per_share", D, PS, _g(
        "EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "ifrs-full:DilutedEarningsLossPerShare")),
    LineItem("dividends_declared_per_share", "Dividends declared per share", "per_share", D, PS, _g(
        "CommonStockDividendsPerShareDeclared")),
    LineItem("weighted_shares_basic", "Weighted average shares, basic", "shares", D, SH, _g(
        "WeightedAverageNumberOfSharesOutstandingBasic")),
    LineItem("weighted_shares_diluted", "Weighted average shares, diluted", "shares", D, SH, _g(
        "WeightedAverageNumberOfDilutedSharesOutstanding")),
    LineItem("shares_outstanding_cover", "Shares outstanding (cover page)", "shares", I, SH, _g(
        "dei:EntityCommonStockSharesOutstanding"),
        note="Cover-page count as of a date near filing, not the balance-sheet date."),
    LineItem("shares_outstanding_balance", "Shares outstanding (balance sheet)", "shares", I, SH, _g(
        "CommonStockSharesOutstanding")),
    # ---------------------------------------------------------------- balance sheet
    LineItem("cash_and_equivalents", "Cash and cash equivalents", "balance", I, M, _g(
        "CashAndCashEquivalentsAtCarryingValue", "Cash", "ifrs-full:CashAndCashEquivalents"),
        note="Excludes restricted cash (CashCashEquivalentsRestrictedCash... is not an alternate)."),
    LineItem("short_term_investments", "Short-term investments", "balance", I, M, _g(
        "MarketableSecuritiesCurrent", "ShortTermInvestments", "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "ifrs-full:CurrentInvestments")),
    LineItem("receivables", "Accounts receivable, net", "balance", I, M, _g(
        "AccountsReceivableNetCurrent", "ifrs-full:TradeAndOtherCurrentReceivables")),
    LineItem("inventory", "Inventory", "balance", I, M, _g("InventoryNet", "ifrs-full:Inventories")),
    LineItem("current_assets", "Total current assets", "balance", I, M, _g("AssetsCurrent", "ifrs-full:CurrentAssets")),
    LineItem("total_assets", "Total assets", "balance", I, M, _g("Assets", "ifrs-full:Assets")),
    LineItem("accounts_payable", "Accounts payable", "balance", I, M, _g(
        "AccountsPayableCurrent", "ifrs-full:TradeAndOtherCurrentPayables")),
    LineItem("current_liabilities", "Total current liabilities", "balance", I, M, _g(
        "LiabilitiesCurrent", "ifrs-full:CurrentLiabilities")),
    LineItem("long_term_debt_current", "Long-term debt, current portion", "balance", I, M, _g(
        "LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent")),
    LineItem("long_term_debt_noncurrent", "Long-term debt, noncurrent", "balance", I, M, _g(
        "LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations", "ifrs-full:NoncurrentPortionOfNoncurrentBorrowings")),
    LineItem("long_term_debt_total", "Long-term debt incl. current portion", "balance", I, M, _g("LongTermDebt"),
             note="Used for total debt only when neither the current nor the noncurrent portion is reported."),
    LineItem("commercial_paper", "Commercial paper", "balance", I, M, _g("CommercialPaper")),
    LineItem("short_term_borrowings", "Short-term borrowings", "balance", I, M, _g(
        "ShortTermBorrowings", "ifrs-full:ShorttermBorrowings")),
    LineItem("total_debt", "Total debt", "balance", I, M, (),
             compute=(Term(1, "long_term_debt_current", False), Term(1, "long_term_debt_noncurrent", False),
                      Term(1, "commercial_paper", False), Term(1, "short_term_borrowings", False)),
             compute_when="always",
             note=("Sum of reported debt components. Requires at least one long-term debt component; when "
                   "neither portion is reported, LongTermDebt (incl. current) is used. Operating lease "
                   "liabilities are excluded (see EV definitions). DebtCurrent is not used, to avoid double "
                   "counting commercial paper and the current portion.")),
    LineItem("operating_lease_liabilities", "Operating lease liabilities", "balance", I, M, _g(
        "OperatingLeaseLiability", "ifrs-full:LeaseLiabilities")),
    LineItem("total_liabilities", "Total liabilities", "balance", I, M, _g("Liabilities", "ifrs-full:Liabilities")),
    LineItem("preferred_stock", "Preferred stock (carrying value)", "balance", I, M, _g(
        "PreferredStockValue", "PreferredStockValueOutstanding")),
    LineItem("noncontrolling_interest", "Noncontrolling interest", "balance", I, M, _g(
        "MinorityInterest", "ifrs-full:NoncontrollingInterests")),
    LineItem("equity_parent", "Shareholders' equity attributable to parent", "balance", I, M, _g(
        "StockholdersEquity", "ifrs-full:EquityAttributableToOwnersOfParent")),
    LineItem("equity_total", "Total equity incl. noncontrolling interest", "balance", I, M, _g(
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "ifrs-full:Equity"),
        compute=(Term(1, "equity_parent"), Term(1, "noncontrolling_interest", False)), compute_when="fallback",
        note="Reported total equity; otherwise equity attributable to parent + noncontrolling interest (if reported)."),
    LineItem("net_debt", "Net debt", "balance", I, M, (),
             compute=(Term(1, "total_debt"), Term(-1, "cash_and_equivalents"), Term(-1, "short_term_investments", False)),
             compute_when="always", note="Total debt - cash and equivalents - short-term investments (if reported)."),
    LineItem("invested_capital", "Invested capital", "balance", I, M, (),
             compute=(Term(1, "total_debt", False), Term(1, "equity_total"), Term(-1, "cash_and_equivalents")),
             compute_when="always", note="Total debt + total equity - cash and equivalents (CAP-CO-07 documented variant)."),
    LineItem("operating_working_capital", "Non-cash working capital", "balance", I, M, (),
             compute=(Term(1, "current_assets"), Term(-1, "cash_and_equivalents"), Term(-1, "short_term_investments", False),
                      Term(-1, "current_liabilities"), Term(1, "long_term_debt_current", False), Term(1, "commercial_paper", False),
                      Term(1, "short_term_borrowings", False)),
             compute_when="always",
             note="(Current assets - cash - short-term investments) - (current liabilities - short-term debt); used for FCFF."),
    LineItem("liabilities_and_equity", "Total liabilities and equity", "balance", I, M, _g(
        "LiabilitiesAndStockholdersEquity", "ifrs-full:EquityAndLiabilities")),
    LineItem("equity_method_investments", "Equity-method investments", "balance", I, M, _g("EquityMethodInvestments")),
    LineItem("pension_funded_status", "Defined benefit plans, funded status", "balance", I, M, _g(
        "DefinedBenefitPlanFundedStatusOfPlan"),
        note="Negative when underfunded (XBRL sign as reported)."),
    # ---------------------------------------------------------------- cash flow
    LineItem("cfo", "Net cash from operating activities", "cash_flow", D, M, _g(
        "NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "ifrs-full:CashFlowsFromUsedInOperatingActivities")),
    LineItem("capex", "Capital expenditure (PP&E payments)", "cash_flow", D, M, _g(
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
        note="Positive = cash paid. PaymentsToAcquireProductiveAssets (broader) is not an alternate."),
    LineItem("cfi", "Net cash from investing activities", "cash_flow", D, M, _g(
        "NetCashProvidedByUsedInInvestingActivities", "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
        "ifrs-full:CashFlowsFromUsedInInvestingActivities")),
    LineItem("cff", "Net cash from financing activities", "cash_flow", D, M, _g(
        "NetCashProvidedByUsedInFinancingActivities", "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
        "ifrs-full:CashFlowsFromUsedInFinancingActivities")),
    LineItem("depreciation", "Depreciation", "cash_flow", D, M, _g("Depreciation")),
    LineItem("amortization_of_intangibles", "Amortization of intangible assets", "cash_flow", D, M, _g(
        "AmortizationOfIntangibleAssets")),
    LineItem("depreciation_amortization", "Depreciation, depletion and amortization", "cash_flow", D, M, _g(
        "DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization",
        "ifrs-full:DepreciationAndAmortisationExpense"),
        compute=(Term(1, "depreciation"), Term(1, "amortization_of_intangibles")), compute_when="fallback",
        note=("Reported total D&A; otherwise Depreciation + AmortizationOfIntangibleAssets (computed, flagged; "
              "omits other amortization a filer may include in an extension concept).")),
    LineItem("share_based_compensation", "Share-based compensation", "cash_flow", D, M, _g(
        "ShareBasedCompensation", "AllocatedShareBasedCompensationExpense")),
    LineItem("dividends_paid", "Dividends paid", "cash_flow", D, M, _g(
        "PaymentsOfDividends", "PaymentsOfDividendsCommonStock", "ifrs-full:DividendsPaidClassifiedAsFinancingActivities")),
    LineItem("share_repurchases", "Repurchases of common stock", "cash_flow", D, M, _g("PaymentsForRepurchaseOfCommonStock")),
    LineItem("free_cash_flow", "Free cash flow (CFO - capex)", "cash_flow", D, M, (),
             compute=(Term(1, "cfo"), Term(-1, "capex")), compute_when="always",
             note="Convention: FCF = net cash from operating activities - PP&E payments."),
)

ITEMS: dict[str, LineItem] = {i.key: i for i in LINE_ITEMS}


def all_concepts() -> list[str]:
    seen: list[str] = []
    for item in LINE_ITEMS:
        for c in item.concepts:
            if c not in seen:
                seen.append(c)
    return seen


def validate_mapping() -> list[str]:
    """Static checks run by the tests: unique keys, known statements, terms reference known items,
    no concept mapped to two items (which would double count)."""
    problems = []
    if len(ITEMS) != len(LINE_ITEMS):
        problems.append("duplicate line-item keys")
    owner: dict[str, str] = {}
    for item in LINE_ITEMS:
        if item.statement not in STATEMENTS:
            problems.append(f"{item.key}: unknown statement {item.statement}")
        if item.period_type not in (D, I) or item.unit_kind not in (M, PS, SH):
            problems.append(f"{item.key}: bad period_type/unit_kind")
        if not item.concepts and not item.compute:
            problems.append(f"{item.key}: neither concepts nor compute")
        for t in item.compute:
            other = ITEMS.get(t.item)
            if other is None:
                problems.append(f"{item.key}: term {t.item} unknown")
            elif other.period_type != item.period_type:
                problems.append(f"{item.key}: term {t.item} has a different period type")
        for c in item.concepts:
            if c in owner:
                problems.append(f"{c} mapped to both {owner[c]} and {item.key}")
            owner[c] = item.key
    return problems
