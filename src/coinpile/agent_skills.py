"""Skills: step-by-step playbooks for common money jobs. The agent sees the list in its instructions and loads one with use_skill when the
user's question fits; the steps name the tools to call, what to calculate and how to answer. They are guidance, not code: the harness still
checks every figure and every change is still asked first."""

SKILLS = {
    "monthly_review": ("A check-up of this month: in vs out, budgets, bills, goals and anything that needs attention.", """
1. get_overview and get_alerts: net worth, what is due, active alerts.
2. cashflow (months 3): this month so far against the last full months; note the savings rate.
3. firefly_budgets for this month, and spending_by_category from the 1st to today: which budgets are over or close (spent / limit with calculate).
4. upcoming_bills (days to the end of the month): what is still to pay, against the cash in checking (list_accounts).
5. list_goals: piggy banks behind schedule (still needed vs months left: savings_plan with by_date).
Answer: one line verdict (on track / tight / short), then 3 to 5 bullets with numbers, then up to 2 next steps. Offer to save it as a note (write_note).
"""),
    "debt_payoff": ("A plan to pay off cards and loans: order, monthly amount, payoff dates and interest.", """
1. list_accounts: every credit and loan account with its balance. Ask for the APR and minimum payment of any debt you do not know; never assume them.
2. For each debt: loan_payoff with its minimum payment.
3. Avalanche (highest APR first) saves the most interest; snowball (smallest balance first) gives quick wins. Work out the extra money per month
   from cashflow (average net) and say how much of it could go to debt.
4. loan_payoff with extra_payment on the target debt: months sooner and interest saved.
Answer: the order, the monthly amount for each, the payoff dates and the total interest saved, with the tool results as the source of every number.
Offer to save the plan as a note, and to set up a piggy bank or budget for it if the user wants (both are asked first).
"""),
    "emergency_fund": ("How many months of spending the savings cover, and how to reach a target.", """
1. cashflow (months 6): average money out per month (spending, without transfers).
2. savings_summary: free savings (not in any piggy bank).
3. calculate: months covered = free savings / average monthly out.
4. Common targets are 3 to 6 months of spending. savings_plan for the gap: by a date the user names, or at an amount they can save (from the cashflow average net).
Answer: months covered now, the target, the gap and the monthly amount or date. Offer an "Emergency fund" piggy bank (asked first).
"""),
    "subscription_audit": ("Everything that repeats: what it costs a year, what is missing or new, and what could go.", """
1. list_subscriptions: monthly cost of each; calculate the yearly total.
2. find_recurring_payments: repeating payments not tracked yet.
3. get_alerts: missing subscriptions (expected but not seen).
4. search_transactions for each larger one over the last 6 months to spot price increases (compare the amounts with calculate).
Answer: the yearly total, the top 5 by cost, price increases, untracked ones, missing ones. Suggest what to review; offer to add untracked ones (asked first).
"""),
    "goal_plan": ("Getting a goal (a piggy bank) funded: monthly amount, date, and where the money comes from.", """
1. list_goals: target, saved, still needed and target date of the goal; piggy_bank_history to see how steadily it has been funded.
2. savings_plan: monthly amount needed for the target date, or the date at the current pace.
3. cashflow and monthly_plan: whether that monthly amount fits; savings_summary for free savings that could cover part of it now.
Answer: monthly amount, date, and a realistic source. Offer to note it on the piggy bank (write_note with piggy_id) or change its target or date (asked first).
"""),
    "big_purchase": ("Can I afford something, and what is the best way to pay for it.", """
1. can_i_afford with the price: free savings and how long saving would take.
2. project_balance (days 60) for the account it would come from: the lowest balance after the purchase (calculate).
3. If financing is an option and the user gives an APR: calculate pmt for the monthly payment and total interest (payment * months - price).
Answer: yes / not yet / only with financing, the numbers behind it, and the safest way. Offer a piggy bank for it (asked first).
"""),
    "budget_setup": ("Set realistic monthly budgets from real spending.", """
1. spending_by_category for each of the last 3 full months; calculate the average per category.
2. firefly_budgets for this month: which budgets exist already.
3. Propose an amount per main category (the average, rounded with calculate), and say what the total is against average income (list_income, cashflow).
Then, only for the ones the user agrees to, set_budget for this month (each is asked first).
"""),
    "idle_cash_and_cds": ("Money sitting in checking or low-interest accounts, CDs maturing, and current rates.", """
1. savings_summary and maturity_outlook: CDs and when they mature; free savings.
2. list_accounts: cash in checking beyond about one month of spending (cashflow average out).
3. web_search (time_range month) for current high-yield savings and CD rates; read_webpage the best source; give the source and date.
4. calculate the yearly interest difference (amount * rate difference).
Answer: how much is idle, what it could earn instead, and options (a ladder, a high-yield account). Offer to mark an account as a CD in Coinpile (asked first).
"""),
    "spending_spike": ("Why spending was higher than usual, and what to do about it.", """
1. cashflow (months 4): which month and by how much (calculate the difference against the average).
2. spending_by_category for that month and the month before: the categories that grew most.
3. search_transactions in those categories for the month, sorted amount_desc: the largest payments and any one-offs.
Answer: the 2 or 3 causes with amounts, one-off vs repeating, and one practical step.
"""),
}


def listing() -> str:
    return "\n".join(f"- {k}: {v[0]}" for k, v in SKILLS.items())


def load(name: str) -> dict:
    s = SKILLS.get(str(name or "").strip())
    if not s:
        return {"error": f"no skill {name!r}. Skills: {', '.join(SKILLS)}"}
    return {"skill": name, "what": s[0], "steps": s[1].strip(), "rules": "Every figure from a tool or calculate. Changes are asked first. Say when a number is an estimate."}
