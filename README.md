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

It cannot move money or change Firefly III. It can write notes (on a piggy bank when the note is about one) and add a subscription only when you ask.
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
- Addresses a person types (a model server, a calendar, a chat webhook, the connector) cannot reach the container itself or the cloud metadata address; your home network is allowed.
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

PayPal, Venmo and Cash App CSVs put in `/data/people/<your Firefly III user id>-imports/` are imported into Coinpile accounts within a minute, then moved to `done/` (or `failed/` if the file is not one of those exports). Uploading them on the Accounts tab does the same.

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
