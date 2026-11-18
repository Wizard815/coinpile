# Coinpile

A simple, friendly view of your **Firefly III**, with an **agent** you can ask about your money, and a remote control for the **Plaid connector** (plaid-proxy, from [plaid-firefly-connector](https://github.com/Wizard815/plaid-firefly-connector)).

Firefly III stays in charge of your data. Coinpile reads it every few minutes and shows it in one place (it took over the screens of the old Goblin Bank app):

- **Overview**: net worth, what you owe on cards, subscriptions per month, bills in the next 30 days, spending this month and a forecast, where the money sits, latest movement. Panels can be moved, resized and hidden (Arrange panels).
- **Money movement**: search and filter every transaction.
- **Accounts**: every Firefly III asset account, credit card and liability, with balance history (net worth has a past from day one: it is read from Firefly III).
- **Subscriptions**: your Firefly III bills, plus the subscription catcher (repeating payments found in your transactions that are not a bill yet).
  Repeats: daily, weekly, every 2 or 4 weeks, monthly, every 2, 3 or 6 months, yearly, every 2 years, or any custom "every N days/weeks/months/years".
  **Ends**: never (the default), on a date, or **after a number of payments**: "3 more, of 4 in all" for PayPal Pay in 4 shows "payment 2 of 4" and counts
  down; after the last one it moves to **Finished**, kept so you can look back (Extend or Delete it there). The end date also goes to the Firefly III bill.
  **Missing subscriptions**: when a subscription's usual payment does not show up (3 days of grace), it is called out on this page, on the Overview and in
  notifications. With "Paid from" set, that account is checked; without it, every account is, by the payee's name, once that payee has been seen before.
- **Savings**: your savings accounts from Firefly III, free vs. set-aside money, and CDs and bonds. A CD is an account: count any account as savings
  and say what it is (CD, money market, bond, HSA, 529...) with its APY, opened date, maturity and term (kept in Coinpile; the balance stays Firefly III's).
  Goals are your Firefly III piggy banks; goals made in Coinpile before that are listed as "older" until you close them or make them piggy banks.
- **Piggy banks**: your Firefly III piggy banks with progress, what is still needed, by when, which account holds the money, and their history. With the
  Piggy banks switch on: make new ones, change name, target, date and notes, and put money in or take it out. Firefly III 6.7.9 has a bug that stops any
  app changing a piggy bank's amount through its API; Coinpile then says so and links straight to that piggy bank in Firefly III. Piggy banks that follow a
  Coinpile goal are left to the goal.
- **Budgets**: Firefly III's budgets, then the monthly plan (budgets per category, bills, what is spent, what is left) and income. Income comes from
  Firefly III: its recurring deposits, and steady deposits found in its transactions.
- **Notes**: search, filter (pinned, mine, by the agent, by tag), tags, templates (monthly review, checklist, big purchase), a read view with headings,
  lists and tick-off checklists, download as Markdown, and the agent writing a note for you from your real numbers. A note can be about a piggy bank:
  it then shows on that piggy bank, and the agent reads the piggy bank and its notes together ("Discuss with agent").
- **Agent**: chat with a model (a local one such as Ollama by default) that can look up your accounts, transactions, subscriptions, savings and bills, and write notes.
- **Settings, Banks**: your banks at the Plaid connector: link a bank, fix a bank sign-in, pull now.

- **Investments**: every account you track as an investment, with a total. Mark any account (a Firefly III brokerage or 401k, an account from the Plaid connector, one you keep in Coinpile) as Investment or Retirement, on the Investments tab or on the account's page; it then shows here and under Investments on the Overview (Firefly III is not changed). Sources that know positions plug in: Robinhood now, Foliofox planned.
- **Robinhood MCP**: Settings, Robinhood, Connect Robinhood: you sign in on Robinhood's own site (its official MCP server, `https://agent.robinhood.com/mcp/trading`, for the Agentic Trading account) and Coinpile gets permission to read. Coinpile asks only for reading and can only call read tools (portfolio and positions); there is no code path to place, cancel or change an order. Its value is recorded each time it is read, so Investments shows value by month from then on. Robinhood's own documentation of the server was not available to test against; the sign-in follows the MCP standard (OAuth 2.1 with PKCE and app registration), and a token can be pasted instead if needed.
- **Robinhood**: positions from Robinhood's activity-report CSV, or the optional live/agent connector containers. Coinpile only (Firefly III cannot hold stock positions).
- **Sharing**: link with other Coinpile people (others signed in to the same Coinpile with their own Firefly III login). Share a goal (each sets money aside, notes thread), or let them see one of your Firefly III accounts. Nothing is shared until you pick it, one item at a time.
  * A goal someone offers you is not yours until you accept: if they take it back or the link ends first, it is gone, and until then it is left out of the Orb, API tokens, webhooks and the agent. One person can have at most 20 offers waiting for you.
  * If another member adds a third person to a shared goal, that person does not get your amount or notes until you press "Share with them too" on the goal.
  * Outside Coinpile (the Orb, API tokens, webhooks, a cloud model, MCP), a shared goal's progress and "still needed" count only your own part, so the others' amounts cannot be worked out from them.
  * An account you delete in Coinpile, or close in Firefly III, stops being shared (and stops being joint) on the other side.
- **Joint accounts at the Plaid connector** (Settings, Banks): ask to share one of your bank accounts with another person on the connector;
  no admin is needed. They accept (as a new account in their Firefly III, linked to one of their own accounts that is not already used, or
  skipped) or decline, and either can stop later (you with Stop, they with Leave). Only accepted accounts reach their Firefly III. The
  connector's README has the details and what happens at the edges.
- **Joint accounts** (Settings, Sharing): a bank account you share with a linked person is in both your Firefly III (accepted at the Plaid connector,
  or set by its admin). One of you offers "my account X is joint with you, I count 50%"; the other picks their copy of it (Coinpile
  suggests it by its last digits). From then on each side counts only its share in net worth and savings, each Coinpile sends the other the
  balance its own Firefly III shows and what that person moved in, and both see a settle-up line ("bob owes you 137.50") and whether the two
  Firefly III copies agree. Either can stop it. Coinpile only: nothing is written to Firefly III, and every number comes from each person's own Firefly III.
  * **What you put in** counts Firefly III transfers from your own bank accounts, and bank-feed pairs: a deposit into the joint account matched
    by a withdrawal of the same amount from one of your own bank accounts within 3 days. The withdrawal has to read like a transfer (its
    category is a transfer, or its text says transfer, Zelle, ACH and the like, or names the joint account), and a deposit that names the
    other person ("Zelle from Sam") is theirs. Each withdrawal matches one deposit only. Card purchases, ordinary bills of the same
    amount, other joint accounts and pending lines are not counted, so the other person's money is not counted as yours.
  * Only bank accounts, cards and loans can be joint. The two shares must add up to 100% (each between 0.01 and 99.99); an acceptance that
    does not is ended on both sides. Once an account is tied as joint, the split cannot be changed on the account's own page by one side.
  * Offering the account to someone else withdraws the earlier offer. Unlinking a person, or Forget me, ends every joint account with them and
    both sides count the whole account again. One person can have at most 20 offers waiting for you.
  * **The agent and joint accounts:** your own side (your account, your share, what you put in) is always available to it. The other
    person's side (what they put in, their balance, what they call their copy, the settle-up line) and anything else they show you reach the
    agent only when the model runs on your own machine. A cloud model, or another app over MCP, gets "withheld" instead, and the joint
    settle-up skill asks you for their total.
- **Home Assistant & API**: read-only, scoped, revocable tokens for Home Assistant, scripts and the Pondering Orb.

### Read from Firefly III, or kept in Coinpile

Coinpile reads everything Firefly III has, so you set it up once, in Firefly III:

| From Firefly III (read only, refreshed every few minutes) | Shown as |
|---|---|
| Accounts, balances, transactions, net worth history | Overview, Accounts, Money movement |
| Bills | Subscriptions ("Firefly III bill"); a payment Firefly III linked to the bill counts as paid, whatever the bank called it |
| Recurring transactions | payments: Subscriptions ("Firefly III recurring"), with Firefly III's "repeat N times" or "until" as the countdown (pay in 4: payment 2 of 4); deposits: Income on Budgets |
| Budgets and budget limits | Budgets page, "Budgets in Firefly III" (limit, spent, left, auto-budgets), for any month |
| Piggy banks | Piggy banks page, with their history |

What Firefly III has no place for stays in Coinpile, and is never sent anywhere: notes (also the ones about a piggy bank); which account is a CD
and its APY and maturity; the per-category plan; who pays a subscription, which account pays it, its reminder, its category and the size of its plan
(these can be set on Firefly III bills and recurring transactions too: their name, amount, schedule and end stay Firefly III's); nicknames; alerts.

Firefly III has no websocket or inbound hook: other apps change it only through its API, which is what the switches below use. Not offered, on purpose:
Coinpile income as Firefly III recurring deposits (Firefly III would make deposits on its own and duplicate the real ones your bank sends), and
category budgets as Firefly III budgets (Firefly III budgets count only transactions given that budget, not a category).

### What Coinpile changes in Firefly III

**Nothing, unless you switch it on** (Settings, Firefly III, Send to Firefly III). Each part is separate:

| Switch | What goes into Firefly III |
|---|---|
| Goals as piggy banks | each goal becomes a piggy bank holding what you set aside for it on Firefly III accounts; closing or deleting the goal removes it |
| Piggy banks | from the Piggy banks page: new piggy banks, their name, target, date and notes, and money put in or taken out |
| Subscriptions as bills | a subscription you add or accept becomes a bill; editing or deleting it changes the bill (daily ones stay in Coinpile) |
| Categories | a category you set on a transaction; "always" also makes a Firefly III rule in a rule group called Coinpile |
| Accounts and transactions | new accounts; transactions you add to a Firefly III account; bank or PayPal/Venmo/Cash App CSVs you import into one (rows already there are skipped) |

Only things Coinpile made are ever changed or removed. Firefly III's own piggy banks count as money already set aside, so Coinpile's "free to use" money never counts it twice.

What Firefly III has no place for always stays in Coinpile: savings APY and maturity, notes, chats, nicknames, minimum-balance warnings, who pays a subscription, reminders, Robinhood, PayPal/Venmo/Cash App and cash accounts you keep in Coinpile only.

A bill in Firefly III is sent only what you changed here: its amount range and its own start date stay as they are, and a change
to Coinpile's own details (paid from, who pays, reminder, category, plan size) sends nothing. A piggy bank Firefly III keeps in another
currency is changed in Firefly III itself; all the piggy banks on an account together cannot hold more than the account does. If
Firefly III is unreachable while a goal's piggy bank is updated, the change is tried again later instead of making a second piggy bank.

**A lost answer is not a failure.** If Firefly III does what it was asked but its answer never arrives (a timeout), Coinpile looks
whether the transaction, bill or piggy bank is there before saying it failed, and a later "try again" finds the first one instead of
making a second. A transaction you add twice on purpose is still two.

### How the numbers are worked out

- **Bills already paid.** A Firefly III bill with a payment linked to it since its last due date is not due again until the next period.
  Nothing is due before a subscription's next payment date.
- **Refunds.** Money coming back into a spending category takes the purchase back out of that category (never below zero); it is not
  income. A category first seen on a refund still counts as spending once money is spent in it.
- **Other currencies.** Totals are in Firefly III's primary currency. An account or transaction Firefly III keeps in another currency
  without converting is left out of net worth, spending and forecasts, and an alert says so (switch on "convert to primary currency" in
  Firefly III to include it). The same goes for a bill or recurring payment in another currency (listed with its currency, left
  out of the monthly total and of what is due) and for recurring income (kept, switched off, named with its currency). A subscription
  you add in Coinpile is entered in the primary currency.
- **Budgets.** A Firefly III budget limit that is not exactly the month shown counts for the part of it that falls in the month, by
  days: a year's limit of 12,000 shows about 986 for November, and a week that straddles the month's end counts for its days in the
  month. Such a budget is marked "part of a longer limit". An auto-budget's amount is scaled from its own period (weekly, yearly ...).
- **Money owed to you** counts in net worth but not as cash for paying bills in the balance projection.
- **Paydays** are counted from the income's own date, so a salary on the 31st lands on 28 Feb and then 31 Mar. Two deposits from one
  payer on one day are one payday.
- **CDs.** APY is the yearly yield: 10,000 at 5.00% APY is worth 10,500 after a year. Two CDs maturing on one day each have their own alert.
- **Subscriptions.** A new subscription is not called missing before its first payment. The same subscription kept in Coinpile and
  later added as a Firefly III bill becomes one row. An end date far in the future (year 9999) is read as "no end".
- **A read that looks wrong changes nothing.** If Firefly III answers with no accounts at all, the refresh stops with a message and
  nothing is deleted. Rows without an id or a usable date are skipped; a value that is not a number is read as 0.
- **Dates and amounts** are checked when entered: dates as YYYY-MM-DD between 1900 and 2200, amounts up to a trillion.

### Firefly III using Coinpile

Firefly III cannot read other apps, but it can tell them when something changes. Add a **webhook** in Firefly III (Automation, Webhooks) pointing at the address shown under Settings, Firefly III, Instant updates, and paste the webhook's secret into Coinpile: Coinpile then shows a new or changed transaction within seconds instead of at the next read. Coinpile checks Firefly III's signature on every call. If Webhooks is greyed out in Firefly III, the owner turns them on under Administration, Configuration.

## How it fits together

```
bank --Plaid--> plaid-firefly-connector --API--> Firefly III --API--> Coinpile (read)
                       ^                                                  |
                       +------------- link a bank / pull now -------------+
```

## Run it

The container is built by GitHub Actions and published at `ghcr.io/wizard815/coinpile:latest`.

The first time it is published, GitHub makes the package **private**. Either make it public (GitHub, your profile, Packages, coinpile, Package settings, Change visibility) or log Docker in to ghcr.io with a token that can read packages.

### Unraid

Docker, Add Container:

| Field | Value |
|---|---|
| Repository | `ghcr.io/wizard815/coinpile:latest` |
| Network | Bridge |
| Port | container `8000`, host e.g. `8190` |
| Path | container `/data`, host `/mnt/user/appdata/coinpile` |
| Variable `FIREFLY_URL` | how Coinpile reaches Firefly III, e.g. `http://192.168.1.50:8182` (not `localhost`) |
| Variable `TZ` | your time zone, e.g. `America/New_York` |

The app runs as user 10001. If it cannot write to `/data`, run once on the Unraid terminal: `chown -R 10001 /mnt/user/appdata/coinpile`, or add `--user 99:100` under Extra Parameters.

Then open `http://<unraid address>:8190`.

### Any Docker host

See [docker-compose.example.yml](docker-compose.example.yml).

## The agent and your bank connector

When a Plaid connector is set up (Settings, Banks), the agent can look at what is wrong there with **bank_problems**: banks that need
signing in again, a last pull that failed (with Plaid's error), accounts whose Firefly III balance is not the bank's, batches the
connector held back from Firefly III, and accounts that look duplicated. It can then ask to **pull a bank** (quick, or full, which Plaid
bills) or to **match a balance** in Firefly III (the opening balance moves; no transaction is added). Both wait for your Yes. Signing in
to a bank again, approving accounts and releasing a held batch stay with you or the connector's admin.

## AI categories (optional, off by default)

Payments no rule, bank category or keyword could place stay on Misc (or Income). In Categories, **AI categories** lets your chat model
suggest one: many payments go in one message with your exact category names, and an answer that is not one of them is thrown away.
A payment already categorized in Firefly III, or set by hand in Coinpile, is never touched, and nothing is sent to Firefly III: the
guess shows in Coinpile and lasts until a category is set in Firefly III. The model sees the description, payee and amount; a cloud
model is used only if you ticked "allow cloud" on it. **Re-run on all** asks about everything still on Misc/Income;
**Auto-categorize new entries** asks only about payments that just arrived, after each read from Firefly III (at most 80 a time).

## Sign in

Coinpile has no passwords of its own: you sign in with your Firefly III account. Every Firefly III user on that Firefly III gets their own Coinpile, kept apart.

**With a Personal Access Token** (works out of the box): in Firefly III, Options, Profile, OAuth, Personal Access Tokens, Create new token. Paste it on Coinpile's sign-in page. Tokens last up to a year.

**With "Sign in with Firefly III"** (one click, no token to copy):

1. In Firefly III: Options, Profile, OAuth, **Create New Client**. Name: `Coinpile`. Redirect URL: the address you open Coinpile at plus `/auth/callback`, e.g. `http://192.168.1.50:8190/auth/callback`. **Untick Confidential.**
2. Copy the client's ID and set on the container: `FIREFLY_CLIENT_ID` (that ID) and `COINPILE_PUBLIC_URL` (e.g. `http://192.168.1.50:8190`).
3. If your browser reaches Firefly III at a different address than the container does, also set `FIREFLY_PUBLIC_URL` (e.g. `https://firefly.example.com`).

Coinpile renews this sign-in by itself.

**A Coinpile password** (optional): once signed in, set one under Settings, Firefly III, Coinpile password. From then on sign in with your Firefly III email and that password; Firefly III is still read with your token. Only a salted scrypt hash is kept (in `/data/users.db`); wrong guesses are slowed down. Changing it needs the current one and signs out your other browsers. Forgot it? On the sign-in page, **Forgot your Coinpile password?** asks for a Personal Access Token to prove it is you, then lets you choose a new one (and the new token becomes the one Coinpile uses). Tested against Firefly III 6.7.9.

Tokens are stored encrypted with a key kept in `/data/.field_key` (or `FIELD_KEY` if you set one). Back up `/data` as a whole; without that key the stored tokens cannot be read (you would just sign in again).

## Banks (Plaid connector)

Settings, Banks: enter the connector's address, your name there and your connector password (or the login token the connector printed for you). From then on you can see your banks, link a new one in Plaid's window, fix a bank sign-in (relink) and pull your banks now. A newly linked bank's accounts wait for the connector admin's approval before they are sent to Firefly III.

**Moved the connector from Goblin Bank to plaid-proxy?** Nothing changes here: the connector's address (its gateway), your name and your
password or login token are the same after `migrate_from_goblin_bank.sh`, so Coinpile's Banks settings keep working as they are. Banks stay
linked, and transactions keep arriving in Firefly III under the same Personal Access Token without any being sent twice.

Banks that sign you in on their own website (OAuth banks) return you to the address the connector registered with Plaid, not to Coinpile; link those from the connector's own `/link` page or admin page.

## The agent

Settings, Agent models: add a model. A local one (Ollama, LM Studio, llama.cpp: anything OpenAI-compatible) is the default and keeps your data at home. A cloud model must be allowed explicitly per model.

The agent sees what every page shows, through the same code the pages use, so its numbers always match the screens. Firefly III is the main record:

| Ask about | Tools |
|---|---|
| Accounts, one account in depth (CD details, last 30 days, balance history) | list_accounts, account_details |
| Transactions and spending | search_transactions, spending_by_category, spending_forecast |
| Bills, subscriptions, missing payments | list_subscriptions, upcoming_bills, find_recurring_payments, get_alerts |
| Budgets | firefly_budgets (Firefly III's), budget_status and monthly_plan (Coinpile's per-category plan) |
| Income | list_income (Firefly III recurring deposits and steady deposits) |
| Goals (piggy banks) and your notes about them | list_goals, piggy_bank_history, read_note, write_note |
| Savings, CDs, affordability | savings_summary, maturity_outlook, can_i_afford, project_balance |
| Net worth, investments, banks | net_worth_history, list_investments, bank_connections (the Plaid connector) |

**Reading is always allowed. Every change is asked first.** When the agent wants to change something, it does not: it puts a card in the chat
saying exactly what will happen and where ("Record in Firefly III: paid 9.99 "Coffee beans" on 2026-11-13, account Everyday Checking, category
Groceries"), with **Yes, do it** and **No**. Only Yes runs it; your answer goes back into the chat so the agent knows. What it can ask for:

| Change | Where |
|---|---|
| record a transaction (cash, a payment the bank has not sent) | Firefly III |
| set a transaction's category, optionally with a rule for future ones | Firefly III |
| make a piggy bank, change its name, target, date or notes, put money in or take it out | Firefly III |
| set a budget's amount for a month (made if missing) | Firefly III |
| add a subscription, optionally also as a Firefly III bill | Coinpile / Firefly III |
| save a note (on a piggy bank when it is about one), mark an account as a CD with its APY and maturity | Coinpile |

Your Yes is the consent, so an approved change runs even with the matching "send to Firefly III" switch off. It cannot move money between banks.

**The harness: every figure from a tool.** The agent does no arithmetic of its own. Before an answer reaches you, Coinpile checks every amount,
decimal and percentage in it against the tool results and what you typed. A figure that came from nowhere sends the answer back once with
"compute these with calculate"; whatever still is not from a tool is marked under the answer ("⚠ not from a tool: ..."), and a clean answer shows
"✓ every figure from a tool". Tool arguments are checked before a tool runs (a wrong type or a missing value comes back as a plain error the model
can fix; numbers written as "$5,000" are read, and nan or infinity are refused), and calling the same thing twice returns "already done, use
the result above" instead of looping. What does not count as a source: the model's own earlier text (a compacted summary included), an
error that only repeats what the model sent, and today's date as money. A known figure times or divided by 100 is not known; only a
fraction from a tool said as a percentage is (0.013 as 1.3%). Names such as 401k, 529 plan, Form 1040 and W-2 are not figures.

**Questions before changes.** A question shows on one line everything a Yes will write (amount first, then who pays, the account, dates
and notes). Things that cannot work are refused before you are asked (an amount that is not a number, a piggy bank on a card, more money
than the account holds). The same request is asked once, and at most 20 wait for your answer.

**The calculator** takes one expression or a list of labelled ones, percentages (5%), and money functions: pmt (loan payment), fv (savings
growth), nper (months to pay off), compound, pct, round, sqrt and friends. Nothing but arithmetic can run in it, and nothing in it can
keep the server busy: results are limited in size and anything larger is refused. Inside a function or a list a comma between digits is
refused as ambiguous ("max(1,200)"): the model is told to write numbers there without thousands separators and with a space after each comma.

**Skills** are step-by-step playbooks the agent loads for a job and follows, each naming the tools to call and what to work out: monthly
review, debt payoff (avalanche vs snowball), emergency fund, subscription audit, goal plan, big purchase, budget setup, idle cash and CDs
(with current rates from the web), spending spike, **tax prep** (tax_year: income by payer, spending by category, every Firefly III tag's total,
lines that look tax related, then the standard deduction from the web; not tax advice), **paycheck splitter** (paycheck_plan: the next
paycheck, the bills due before the one after it, what each piggy bank needs from it, what is left) and bill negotiation (price rises and a call
script), **year in review**, **insurance and renewals** (yearly bills coming up and whether they rose), **joint settle-up** (shared_expenses:
joint accounts and payments tagged "shared" in Firefly III, who owes whom) and **weekly brief** (week_changes: short enough to be read out; the
Orb can be given it under Sharing as "This week"). The Agent page's **What I can do** lists every skill (with Run) and every tool, marked
"reads" or "asks first", and the MCP address for other assistants. The agent also picks a skill itself when a question fits.

**What Firefly III gives the agent.** Everything Coinpile reads: accounts, balances, transactions with their category, **tags**, **budget**,
**payee** and notes, bills and which payment paid which bill, recurring transactions, budgets and limits, piggy banks and their history, and
net worth history. Search by tag (tag "tax-deductible" in Firefly III and the tax year is one search) or by budget. With the Firefly III webhook
set (Settings, Firefly III) a change there reaches Coinpile at once; the agent can also call refresh_from_firefly when you say you just
changed something.

**Coinpile as an MCP server** (for Pondering Orb, Claude Desktop or any MCP client): make a token with the **mcp** scope (Settings, Home
Assistant & API) and point the client at `https://your-coinpile/api/v1/mcp` with `Authorization: Bearer <token>`. It gets every read tool, the
calculator, and the skills as prompts. Change tools only ask: the request waits under "Waiting for your answer" on Coinpile's Agent page.
The Orb's summary can also share goals (piggy banks), Firefly III budgets and the next paycheck (Settings, Home Assistant & API, Sharing). Two more
options feed the Orb's stock and journal pages: **Holdings** (the stocks you
hold, from Robinhood's activity CSV, MCP or connector: symbol, shares, net
cost, value, one row per account, as last read) and **Daily totals** (spent and
income per day for the last 31 days; transfers between your own accounts and
pending payments don't count; never the transactions themselves).

**Money maths and outside facts.** loan_payoff (months, interest, and what an extra payment saves), savings_plan (monthly amount for a date, or
the date for a monthly amount, with interest), cashflow (in vs out per month and the savings rate) and calculate do the maths exactly.
With a **SearXNG** address in Settings, Agent models, Web search (or `SEARXNG_URL`), web_search looks up current rates, limits and rules and
read_webpage reads a page it found to check a figure; it names the source and date. SearXNG must allow JSON (`search.formats: [html, json]` in
its settings.yml). The agent is told never to put personal details in a search, and a query that holds an email address, an account or
card number or one of your exact balances is not sent. It can only open pages its search returned in the last ten minutes, and only on the
public internet: a page (or a redirect) that leads into your home network or to the server is refused. A page is read up to 2 MB and 30 seconds.

**Which models count as local.** A model is local when its address leads to this machine or your home network. That is decided on where
the address really goes: a number that is a public address in disguise, a look-alike dot, or a local-looking name that resolves to a
public address is not local, so it needs "allow cloud" and gets other people's numbers withheld. If you switch a chat from a local model
to a cloud one, earlier answers (and a summary) that may hold what other people share with you are left out of what the cloud model sees.
A model without tool calling gets a summary of the same data instead. "Discuss with agent" on a piggy bank or a note starts a chat with it attached.

## Settings

| Variable | |
|---|---|
| `FIREFLY_URL` | required: Firefly III's address as the container reaches it |
| `FIREFLY_PUBLIC_URL` | Firefly III's address as your browser reaches it (default: `FIREFLY_URL`) |
| `FIREFLY_CLIENT_ID` | turns on "Sign in with Firefly III" |
| `COINPILE_PUBLIC_URL` | the address you open Coinpile at (needed with `FIREFLY_CLIENT_ID`) |
| `CONNECTOR_URL` | default Plaid connector address for everyone's Banks settings |
| `REFRESH_MINUTES` | default: read Firefly III every 10 minutes (each person can change it) |
| `DATA_DIR` | default `/data` |
| `RH_AGENT_URL`, `RH_LIVE_URL`, `CONNECTOR_SECRET` | the optional Robinhood connector containers |
| `RH_OWNER` | the Firefly III email of the one person those Robinhood connectors belong to (nobody else sees them; without it, nobody does) |
| `ALLOW_LOOPBACK_URLS` | `1` lets addresses people type (model server, calendar, webhook) point at the container itself; off by default |

### Security notes

- Each person's settings and secrets are their own: nothing falls back to the container's environment for a person (except `CONNECTOR_URL` and `REFRESH_MINUTES`, which are not secret).
- Addresses a person types (a model server, a calendar, a chat webhook, the connector) cannot reach the container itself or cloud metadata addresses; your home network is allowed. Pages the agent reads from a web search may only be on the public internet.
- Handles are unique: two people can never be given the same name for sharing.
- "Forget me" affects only that person; a request of theirs still on its way is answered "sign in first".
- Numbers in requests and from another person's Coinpile must be ordinary finite numbers; a malformed message from a linked person is ignored whole.
- **Sign out everywhere** (Settings, Firefly III, Leave) ends your session in every browser and on every device; "…and end API tokens" revokes your API tokens too.
- If Firefly III stops accepting Coinpile's sign-in for you (the token was revoked or ran out), your sessions end within a day instead of lasting up to 30, and your API tokens stop answering after a day, until you sign in to Coinpile again.
- Password sign-in slows down wrong guesses per name and address, so guesses from somewhere else do not lock you out where you are; old failures are forgotten after 15 minutes. Signing in ends whatever session that browser had before.
- "Sign in with Firefly III" can only be finished in the browser that started it.
- A person's name for sharing is never given to someone else after they use "Forget me", and the people they shared with are told.
- What the agent adds (subscriptions, goals, notes) stays in Coinpile: it is never sent to Firefly III without you.
- Uploads over 6 MB are refused before they are read. Pages cannot be framed by other sites.

## Develop

```bash
pip install -e . pytest
```

```bash
python -m pytest -q
```

```bash
FIREFLY_URL=http://127.0.0.1:8080 DATA_DIR=./data UI_DIR=./ui PYTHONPATH=src uvicorn --factory coinpile.web:main --port 8190
```

The tests use a stand-in for Firefly III's API (`tests/fake_firefly.py`) and a scripted model (`devtools/fake_llm.py`).

## Drop folder

PayPal, Venmo and Cash App CSVs put in `/data/people/<your Firefly III user id>-imports/` are imported into Coinpile accounts within a minute, then moved to `done/` (or `failed/` if the file is not one of those exports). Uploading them on the Accounts tab does the same. One file holds up to 20,000 rows (split a longer history); an empty file makes no account. "Export CSV" on Transactions keeps text that looks like a spreadsheet formula as text.

## Webhooks (Coinpile tells other apps)

Settings, Webhooks: add an address and choose what to send. Coinpile POSTs a small JSON message when it happens:

| Event | When |
|---|---|
| `transaction.new` | a new transaction (from Firefly III, a CSV import, or added by hand) |
| `balance.changed` | an account's balance moved |
| `bill.due` | a bill or subscription is due within its reminder days |
| `alert.new` | something needs attention (overdrawn, under your minimum, a missed payment, a CD maturing) |
| `goal.changed` | a goal was added, funded, changed or closed |
| `refresh.done` | Coinpile finished reading Firefly III (with net worth) |

```json
{"id": "89112a21...", "event": "transaction.new", "at": "2026-11-11T10:00:00+00:00", "person": "alice",
 "data": {"account": {"id": "ff:1", "name": "Everyday Checking", "institution": "Huntington", "kind": "cash"}, "date": "2026-11-11", "name": "TACO TRUCK", "amount": -18.4, "currency": "USD", "category": "Food"}}
```

Every message carries `Coinpile-Event` and `Coinpile-Signature: t=<unix time>,v1=<hex HMAC-SHA256 of "<t>.<body>">` (the secret is shown once when you add the webhook). Untick "Include details" to send only the event, without names or amounts. Failed deliveries are retried after 1, 5, 30, 120 and 720 minutes. Only what happens after you add a webhook is sent.

Home Assistant: use a `webhook` trigger with the id from the address (`/api/webhook/<id>`); the message is in `trigger.json`.

## Home Assistant

Settings, Home Assistant & API: make a token that may read `summary` (and `refresh` for an on-demand read of Firefly III). Coinpile shows the Home Assistant YAML. Tokens look like `cp_<n>_...` and are read at `/api/v1/me/{summary|ha|bills|alerts|savings|goals|budgets|transactions}`. For the Pondering Orb, make a token with the `orb` scope and tick what it may see.

## Not yet

- Budgets in Coinpile are Coinpile's own; Firefly III budgets are not read yet.
- Firefly III 6.7 can fail to change a piggy bank's amount (an internal error). Coinpile then replaces the piggy bank it made with a new one holding the new amount.
