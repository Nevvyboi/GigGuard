# GigGuard

**Programmable income smoothing for gig workers, built on Investec programmable banking.**

GigGuard turns the feast or famine of gig income into something that feels
like a steady weekly salary. When money lands, it quietly holds a slice back.
Each week it releases a fixed amount you are allowed to spend, and the
Investec programmable card enforces that limit at the bank itself. Go over
the weekly release and the card simply declines. No nagging notifications, no
willpower required.

![GigGuard dashboard](docs/dashboard-overview.png)

> Sandbox project. No real money moves. The buffer is a ledger, not an
> account. Nothing here is financial advice.

---

## What it is

Three small pieces that work together:

1. A bit of **card code** that runs inside the Investec programmable card IDE.
   It intercepts every spend and asks the backend "is this allowed?".
2. A small **Express backend** that keeps a buffer ledger, watches the account
   for incoming gig payouts, and answers the card in well under a second.
3. A self contained **dashboard** you can open in any browser to see the whole
   thing move, with sliders and buttons to play out income and spending in the
   sandbox.

You can open `frontend/dashboard.html` right now, with no server and no setup,
and the full engine runs in the page.

## The problem it solves

Gig workers do not get paid on a calendar. A good week pulls in R8,000, the
next week pulls in R600. The money that arrives in a burst tends to leave in a
burst, and then the quiet week hurts. Traditional budgeting apps warn you
after the fact. A warning is easy to swipe away at the till.

GigGuard moves the limit down to the card. The cap is not a reminder, it is a
hard decline. The good week funds the quiet week whether or not you remember
to be disciplined.

## How the buffer engine works

Everything is integer cents internally. Rands only appear when a human needs
to read a number. The whole engine is five lines of arithmetic:

```
on each incoming credit:
    toBuffer       = floor(income_cents * bufferPercent / 100)
    spendable      = income_cents - toBuffer
    bufferBalance += toBuffer
    weeklyRelease  = floor(bufferBalance / minWeeks)

on each card spend:
    remaining      = weeklyRelease - spentThisWeek
    decline if      spend_cents > remaining
```

A worked example, the default 30 percent over 4 weeks:

```
R8,000 lands  ->  R2,400 to buffer, R5,600 spendable
                  weeklyRelease = floor(2400 / 4) = R600 a week
                  runway        = 2400 / 600       = 4.0 weeks

spend R200    ->  approved, R400 left this week
spend R500    ->  declined, only R400 left
spend R400    ->  approved exactly to the line, R0 left
```

`bufferPercent` is clamped to between 10 and 60. `minWeeks` is clamped to
between 1 and 12. Flooring always rounds in the safe direction, leaving the
odd cent in the buffer rather than handing it out.

## Architecture

```
        Gig payout lands in the Investec account
                          |
                          v
   poll transactions  +------------------------------+
   ----------------->  |  GigGuard backend            |
   Investec Accounts   |  Express, in memory ledger   |
   API                 |                              |
                       |  withhold bufferPercent      |
                       |  weeklyRelease = buffer/weeks|
                       +------------------------------+
                         ^             |
              /check     |             |   /status
              /record    |             |   /simulate/income
                         |             v
        +----------------------+   +-----------------------+
        |  Investec card code  |   |  Dashboard (one HTML) |
        |  beforeTransaction   |   |  sandbox playground   |
        |  afterTransaction    |   +-----------------------+
        +----------------------+
                         |
                         v
            Card approves or declines at the till
```

## Investec API usage

| Investec surface | How GigGuard uses it |
| --- | --- |
| OAuth token endpoint (`identity.secure.investec.com`) | Client credentials grant with the `accounts` scope. Token is cached until just before it expires. |
| Accounts transactions API (`openapisandbox.investec.com`) | Polled on a schedule to spot incoming CREDITs, which is how income is detected. |
| Card `beforeTransaction` hook | Calls the backend `/check` and declines the spend when it would break the weekly release. |
| Card `afterTransaction` hook | Calls the backend `/record` so approved debits are added to this week's running total. |

## Monetisation

All prices in South African rand. Sandbox build, so none of this is wired to a
payment processor, it is just the intended shape.

| Tier | Price | What you get |
| --- | --- | --- |
| Free | R0 | One card, manual buffer percent, weekly release enforced, dashboard. |
| Standard | R39 per month | Automatic income polling, custom buffer and runway, transaction history. |
| Pro | R99 per month | Multiple income streams, runway forecasting, export, priority support. |
| Pay as you go | R1.50 per payout processed | No monthly fee. You only pay when income actually lands and gets smoothed. |

## Project structure

```
GigGuard/
  card-code/
    main.js          Deployed into the Investec card IDE (before/after transaction)
  backend/
    server.js        Express API: /check /record /poll /setup /status /simulate/income
    package.json
    test.js          Pure logic test suite, no server needed
  frontend/
    dashboard.html   Self contained interactive dashboard, one file
  docs/              Screenshots
  .env.example
  knowledge          Gotchas and learnings from building this
  README.md
  LICENSE
```

## Setup

### 1. Backend

```
cd backend
cp ../.env.example .env      # then open .env and fill in your values
npm install
npm start
```

The server boots with a ready made `demo` user, so you can poke it straight
away:

```
curl localhost:3000/status/demo
```

### 2. Onboard with curl

Configure the demo user (or any user id) with your Investec sandbox details
and your two dials:

```
curl -X POST localhost:3000/setup \
  -H 'content-type: application/json' \
  -d '{
    "userId": "demo",
    "investecClientId": "your-client-id",
    "investecSecret": "your-secret",
    "investecApiKey": "your-api-key",
    "accountId": "your-account-id",
    "bufferPercent": 30,
    "minWeeks": 4
  }'
```

Pretend a payout landed, then watch the buffer fill:

```
curl -X POST localhost:3000/simulate/income \
  -H 'content-type: application/json' \
  -d '{"amountRands": 8000}'

curl localhost:3000/status/demo
```

Pull real sandbox transactions instead of simulating:

```
curl -X POST localhost:3000/poll -H 'content-type: application/json' -d '{"userId":"demo"}'
```

### 3. Card IDE

In the Investec programmable banking card IDE:

1. Paste the contents of `card-code/main.js` into the editor.
2. Add two environment variables in the IDE settings:
   * `GIGGUARD_WEBHOOK_URL` set to your backend's public base URL, for example
     a tunnel like `https://your-tunnel.ngrok.app`.
   * `GIGGUARD_API_KEY` set to the same value you put in the backend `.env`.
3. Save and deploy. Now every tap of the card checks the weekly release first.

### 4. Dashboard

Open `frontend/dashboard.html` in a browser. It is fully self contained and
needs no server. The dashboard is laid out like a weekly statement: one big
"available to spend this week" figure on a paper card, with the buffer, the
weekly release meter, and the runway responding live as you simulate income
and card taps with the sliders and buttons.

When a spend goes over the weekly release, the card declines it and a red
notice appears:

![GigGuard declining an overspend](docs/dashboard-decline.png)

## Tests

The logic suite is pure arithmetic with no server and no network:

```
cd backend
node test.js
```

It prints a tick or a cross for each of the ten checks and exits non zero if
any fail, so it drops straight into CI. The checks cover zero state, the
withholding split, status readout, approvals, the decline at the boundary, a
one cent overspend, stacked income, a zero credit, and a different buffer and
runway setting.

## Safety and guardrails

* **No money moves.** GigGuard never sweeps or transfers funds. The buffer is
  a number, your cash stays in your own account.
* **Fail open.** If the backend is unreachable, slow, or returns an error, the
  card approves rather than stranding you at a till. Wrongly declining a real
  purchase is a worse outcome than a soft cap for one transaction.
* **Integer money.** All arithmetic is in cents. No floats touch a balance.
* **Clamped settings.** Buffer percent stays within 10 to 60, runway within 1
  to 12, no matter what a client sends.
* **No advice.** GigGuard does not tell anyone what to do with money. It is a
  tool for enforcing a limit you set yourself.
* **Sandbox only.** This repo targets the Investec sandbox. Do not point it at
  a live account.

## Knowledge file

`knowledge` holds the ten things that actually cost time while building this,
including the two second card hook budget, the rands versus cents trap between
the API and the card, reading environment variables as `env.NAME` inside the
card IDE, why the after hook only records approved debits, why income is polled
rather than pushed, and why the buffer being a ledger and not an account keeps
this a budgeting tool rather than a regulated deposit business. Read it before
changing the card code.

## License

MIT. See [LICENSE](LICENSE).
