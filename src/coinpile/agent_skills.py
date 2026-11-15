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
    "tax_prep": ("Get ready for taxes: income, deductible-looking spending, tags, and what to gather.", """
1. tax_year (the year asked, default last year): income by category and payer, spending by category, tag totals, lines that look tax related.
2. search_transactions with tag for any tax-like tag (tax-deductible, donation, medical, business) to list them; calculate the totals per kind.
3. web_search (time_range year) for the year's standard deduction and the limits that matter (official IRS pages; read_webpage to confirm), and compare
   with calculate (itemised total vs standard deduction).
Answer: income total and sources, the deductible-looking totals by kind, whether itemising looks worth it, and a short checklist of documents to gather
(W-2/1099 from each payer listed, receipts for the flagged lines). Say plainly this is not tax advice. Offer to save it as a note, and suggest
tagging such transactions in Firefly III (for example tax-deductible) so next year is a one-step search.
"""),
    "paycheck_splitter": ("Split the next paycheck between bills, piggy banks and spending money.", """
1. paycheck_plan: the next paycheck, the bills due before the one after it, what each piggy bank needs from it, what is left.
2. list_accounts: is the money for those bills in the account they come out of? (calculate the gap per account if not).
3. firefly_budgets and spending_by_category for this month: does "left for spending" cover what is usually spent in the same number of days
   (calculate daily average * days)?
Answer: a short table: bills (with dates), each piggy bank, spending money, and anything short. Suggest a fixed split if the user wants one
(e.g. a percentage to savings) worked out with calculate. Offer to save it as a note.
"""),
    "bill_negotiation": ("Find bills and subscriptions whose price went up, and prepare to call them.", """
1. list_subscriptions, then search_transactions for each one over the last 12 months (sort date_asc): the first and the latest amount; calculate the increase and its yearly cost.
2. web_search for current promotions or the going rate for that service (no personal details in the query).
Answer: the bills that went up, by how much a year, the going rate found (with source and date), and a short call script per bill
(what you pay, what new customers pay, ask for the retention rate, ready to cancel). Offer to save it as a note.
"""),
    "year_in_review": ("The year in money: what came in and went out, what was saved, how net worth moved, and the biggest changes.", """
1. tax_year (the year, default last year): income and spending by category.
2. net_worth_history (days 366 for a past year, or from 1 January): start, end, change, lowest and highest.
3. cashflow (months 12): the months with the most and least left over; calculate the savings rate for the year (income - spending) / income.
4. list_goals: what the piggy banks reached; list_subscriptions: the yearly cost of subscriptions (calculate).
Answer: a short story of the year with the numbers (in, out, saved, net worth change), the 3 biggest categories, the best and worst month,
and two things to try next year. Offer to save it as a note.
"""),
    "insurance_and_renewals": ("Yearly and twice-yearly bills coming up (insurance, registrations, memberships, domains) and whether they went up.", """
1. list_subscriptions: the ones repeating yearly, every 6 months or every 2 years; upcoming_bills (days 120) for their dates.
2. search_transactions for insurance, premium, registration, renewal, membership, license, domain over the last 2 years: renewals not tracked
   yet, and last year's amount against this year's (calculate the change).
3. savings_plan: the monthly amount to set aside so the next big renewal is covered by its date.
Answer: a dated list of renewals with amounts and changes, the ones not tracked yet, and the monthly set-aside. Offer a "Renewals" piggy bank
and to add untracked ones as subscriptions (both asked first). Shopping around near renewal often saves money: say so for the big ones.
"""),
    "joint_settle_up": ("Who owes whom for shared costs over a period.", """
1. shared_expenses for the period; its linked_joint_accounts are joint accounts both people tied together in Coinpile: each shows what each
   person moved in this month from their own accounts, the agreed split, and who owes whom for it ("settle"), plus whether both Firefly III copies
   show the same balance. Then (ask which period if unclear; default the last 30 days): spending from joint accounts (already shared), and
   payments from your own accounts tagged "shared" in Firefly III, split at the agreed share (ask if it is not 50/50).
2. shared_with_me: what the other person shows you and the joint accounts' numbers on both sides (only with a model on your own machine; with a
   cloud model their numbers are withheld: then ask the user for the other person's total). For payments outside joint accounts, ask for the other
   person's total for what they paid for both (it is in their Firefly III, not here), then calculate the difference.
Answer: one line ("Sam owes you 42.10"), then the list it came from. Suggest tagging shared payments "shared" in Firefly III so this stays one
step. Offer to save the settle-up as a note.
"""),
    "weekly_brief": ("What changed this week, short enough to be read out (the Pondering Orb can read it every week).", """
1. week_changes: money in and out against last week, the biggest payments, new repeating payments, alerts, net worth change, bills next week.
2. get_alerts for anything critical.
Answer in 4 to 6 short spoken sentences, numbers rounded sensibly (use calculate for any rounding or difference), the most important first,
ending with the one thing to do this week. No tables or lists: it may be read aloud.
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
