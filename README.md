# Coinpile

A simple, friendly view of your **Firefly III**, with an **agent** you can ask about your money, and a remote control for the **Plaid connector** ([plaid-firefly-connector](https://github.com/Wizard815/plaid-firefly-connector)).

Firefly III stays in charge of your data. Coinpile reads it every few minutes and shows it the way the old Goblin Bank app did:

- **Overview**: net worth, what you owe on cards, subscriptions per month, bills in the next 30 days, spending this month and a forecast, where the money sits, latest movement. Panels can be moved, resized and hidden (Arrange panels).
- **Money movement**: search and filter every transaction.
- **Accounts**: every Firefly III asset account, credit card and liability, with balance history.
- **Subscriptions**: your Firefly III bills, plus the subscription catcher (repeating payments found in your transactions that are not a bill yet).
- **Savings**: Firefly III piggy banks, and Coinpile's own goal planner (set money aside for goals, free vs. promised money, CDs and when they mature).
- **Budgets & notes**: monthly plan, incomes, notes.
- **Agent**: chat with a model (a local one such as Ollama by default) that can look up your accounts, transactions, subscriptions, savings and bills, and write notes.
- **Settings, Banks**: your banks at the Plaid connector: link a bank, fix a bank sign-in, pull now.

Rule of thumb: **change data in Firefly III** (transactions, categories, accounts, bills, piggy banks). Coinpile keeps only its own things: goals, notes, savings you track by hand, chats, nicknames.

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

Coinpile renews this sign-in by itself. Tested against Firefly III 6.7.9.

Tokens are stored encrypted with a key kept in `/data/.field_key` (or `FIELD_KEY` if you set one). Back up `/data` as a whole; without that key the stored tokens cannot be read (you would just sign in again).

## Banks (Plaid connector)

Settings, Banks: enter the connector's address, your name there and your connector password (or the login token the connector printed for you). From then on you can see your banks, link a new one in Plaid's window, fix a bank sign-in (relink) and pull your banks now. A newly linked bank's accounts wait for the connector admin's approval before they are sent to Firefly III.

Banks that sign you in on their own website (OAuth banks) return you to the address the connector registered with Plaid, not to Coinpile; link those from the connector's own `/link` page or admin page.

## The agent

Settings, Agent models: add a model. A local one (Ollama, LM Studio, llama.cpp: anything OpenAI-compatible) is the default and keeps your data at home. A cloud model must be allowed explicitly per model.

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

## Not yet

- Writing back to Firefly III (for example, turning a found subscription into a Firefly III bill, or recategorising) is done in Firefly III for now.
- Budgets in Coinpile are Coinpile's own; Firefly III budgets are not read yet.
- Goals do not move money into piggy banks; piggy banks are shown read-only.
