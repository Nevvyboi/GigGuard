# GigGuard

**Income smoothing for gig workers, sold through the fleets and platforms they already drive for, on Investec programmable banking.**

GigGuard turns the feast or famine of gig income into something that feels like
a steady weekly salary. When a payout lands it quietly holds a slice back, and
each week it releases a fixed amount you may spend. The Investec programmable
card enforces that limit at the bank: go over the weekly release and the card
declines at the till. When the backend answers inside the card's roughly two
second budget it is a hard decline; by deliberate design it fails open if the
backend is unreachable, so the cap is a strong self imposed limit, not an
unbreakable lock. Every rival in the field predicts or advises. GigGuard is the
only one that enforces.

The business is B2B2C. A fleet operator or gig platform offers GigGuard to its
drivers as a retention perk and pays per active seat, and a stubbed per payout
fee covers the rest (see [Monetisation](#monetisation)). There are two surfaces,
both running live against the same multi tenant backend.

**The driver's dashboard**, lumpy gig income smoothed into a steady weekly
release, then a spend over the cap declined:

![GigGuard driver dashboard](docs/dashboard-demo.gif)

**The partner's fleet console**, every driver's buffer plus the bill the fleet
pays (per active seat, plus the per payout smoothing fee):

![GigGuard partner console](docs/partner-demo.gif)

> Sandbox project. No driver money moves; the buffer is a ledger, not an
> account. The only money that changes hands is our own fee, and even that is a
> stub. Nothing here is financial advice.

---

## What it is

Four pieces that work together:

1. **Card code** for the Investec programmable card IDE. On every tap it asks
   the backend "is this allowed?", passes the card id so one backend can serve a
   whole fleet, and reports approved debits back.
2. A multi tenant **Express backend** that keeps a buffer ledger per driver,
   watches each account for incoming payouts, answers the card in well under a
   second, bills the fleet per active seat, takes a stubbed fee per smoothed
   payout, and persists everything to a JSON file so a restart does not wipe a
   ledger.
3. A **driver dashboard** (`frontend/dashboard.html`). Open it on its own and a
   built in engine runs the whole thing offline; point it at a running backend
   and it flips to a live mode that drives the real API.
4. A **partner console** (`frontend/partner.html`) for the fleet operator: every
   driver's buffer, the per seat bill, the smoothing revenue, and a one click
   onboard, all read live from the backend.

The driver dashboard opens standalone with no server (its only external request
is two web fonts from Google Fonts). The partner console needs the backend.

## The problem it solves

Gig workers do not get paid on a calendar. A good week pulls in R8,000, the
next week pulls in R600. The money that arrives in a burst tends to leave in a
burst, and then the quiet week hurts. Traditional budgeting apps warn you
after the fact. A warning is easy to swipe away at the till.

GigGuard moves the limit down to the card. The cap is not a reminder you can
swipe away, it is a decline at the till whenever the backend is reachable.
Instead of R8,000 that is gone by Wednesday and a R600 week that hurts, you get
a steady weekly amount you can plan around. Because the money never leaves your
own account, it stays a firm nudge rather than a vault, which is the honest
edge of a self imposed limit.

## Who this is for

GigGuard is for South African gig and informal workers whose pay arrives in
lumps: ride hailing drivers (Uber, Bolt, inDrive), delivery riders, freelancers
paid per invoice, and casual or piece rate labour. They are paid by the trip,
the job, or the week, never on a steady monthly calendar, so a good week and a
lean week sit right next to each other.

Picture the user, an illustration rather than a real interviewee: a Bolt driver
in Johannesburg who clears about R7,000 in a strong week and about R1,500 in a
slow one. The money lands in a burst and tends to leave in a burst, and then
the quiet week is the one that hurts. Rent, airtime, fuel, and food still
arrive on a calendar even when the income does not.

This is a large group. Stats South Africa put informal economy employment at
roughly 5.7 million people in the [Q4 2025 Quarterly Labour Force Survey](https://www.statssa.gov.za/?cat=31),
with hundreds of thousands of active ride hailing and delivery drivers among
them. We did not run a formal user study for this sandbox build, so we are not
claiming survey data. The pain comes from the shape of the work itself: payouts
that are lumpy by design, plus the plain fact that a warning notification is
easy to swipe away at the till. GigGuard is for the person who already knows
they overspend a feast week and wants a limit they set once and then cannot
casually argue their way out of in the checkout queue.

## How the buffer engine works

Everything is integer cents internally. Rands only appear when a human needs
to read a number. The whole engine is five lines of arithmetic:

```
on each incoming credit:
    toBuffer       = floor(incomeCents * bufferPercent / 100)
    spendable      = incomeCents - toBuffer
    bufferBalance += toBuffer
    weeklyRelease  = floor(bufferBalance / minWeeks)

on each card spend:
    remaining      = weeklyRelease - spentThisWeek
    decline if      spendCents > remaining
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
        Gig payout lands in each driver's Investec account
                          |
                          v
   poll transactions  +-------------------------------+   /fleet  +-------------------+
   ----------------->  |  GigGuard backend             | <------- |  Partner console  |
   Investec Accounts   |  Express, per driver ledger   | -------> |  drivers + billing|
   API                 |  persisted to a JSON file     |  /onboard +-------------------+
                       |                               |
                       |  withhold bufferPercent       |   /status      +------------------+
                       |  weeklyRelease = buffer/weeks | <------------- |  Driver dashboard|
                       |  bill per seat + per payout   | -------------> |  offline or live |
                       +-------------------------------+  /simulate     +------------------+
                         ^             |
              /check     |             |  cardId maps each tap to a driver
              /record    |             v
        +----------------------+
        |  Investec card code  |
        |  beforeTransaction   |
        |  afterTransaction    |
        +----------------------+
                         |
                         v
            Card approves or declines at the till
```

## Investec API usage

| Investec surface | How GigGuard uses it |
| --- | --- |
| OAuth token endpoint (`openapisandbox.investec.com/identity/v2/oauth2/token`) | Client credentials grant with the `accounts` scope. Token is cached until just before it expires. The live host is `identity.secure.investec.com/connect/token`. |
| Accounts transactions API (`openapisandbox.investec.com`) | Polled on a schedule to spot incoming CREDITs, which is how income is detected. |
| Card `beforeTransaction` hook | Calls the backend `/check` and declines the spend when it would break the weekly release. |
| Card `afterTransaction` hook | Calls the backend `/record` so approved debits are added to this week's running total. |

## Live sandbox run

This is not a mock. GigGuard runs against the real Investec sandbox. The shared
public sandbox credentials ship in `.env.example`, so you can clone, start the
backend, and poll the sandbox "Mr Smith" account yourself. Every number below
came back from the live Investec API.

![GigGuard live sandbox loop: real OAuth, then 13 real credits polled into the buffer, then the card declining an overspend](docs/live-loop-demo.gif)

```
# 1. Poll the real account: OAuth, then the transactions API
$ curl -X POST localhost:3000/poll -H 'x-api-key: ...' \
       -d '{"fromDate":"2026-03-01","toDate":"2026-06-01"}'
{
  "creditsCounted": 13,
  "skimmedToBuffer": "27871.33",
  "bufferBalance":  "27871.33",
  "weeklyRelease":  "6967.83",
  "window": { "fromDate": "2026-03-01", "toDate": "2026-06-01" }
}

# 2. Thirteen lumpy real credits (STANSAL, STANCOM, refunds, interest)
#    are now one steady weekly release
$ curl localhost:3000/status/demo -H 'x-api-key: ...'
{
  "bufferBalance":     "27871.33",
  "weeklyRelease":     "6967.83",
  "remainingThisWeek": "6967.83",
  "runwayWeeks":       "4.0"
}

# 3. The card beforeTransaction hook checks that weekly release
$ /check  R7,467.83  ->  { "approved": false, "reason": "Over the weekly release. R6967.83 left, this spend is R7467.83." }
$ /check  R250.00    ->  { "approved": true,  "reason": "Within the weekly release. R6717.83 left after this." }
```

The poll window is explicit here because the sandbox demo data sits a few months
in the past. In production the poll runs on a schedule from wherever it last left
off, so you never pass dates by hand.

## Monetisation

All prices in South African rand, all illustrative. The charge is a stub, not a
live payment integration. But this is no longer just a slide: the partner
console shows the bill, and the backend takes the per payout fee on every
smoothed payout, so you can watch the revenue tick up as the demo runs.

### Fleets pay per seat, the main line

The believable payer is not the gig worker, who by definition has the least cash
to spare. It is the partner who already touches the worker and benefits when the
worker keeps working: a fleet operator, ride hailing aggregator, gig
marketplace, or earned wage provider. They offer GigGuard to their drivers as a
retention perk and pay per active seat, roughly **R25 per active driver per
month**. A driver whose rent survives a lean week keeps driving, so smoothing is
a retention tool for the platform, not a favour to the worker, and GigGuard
reaches drivers through the people who already pay them rather than through
expensive consumer ads. The partner console (`frontend/partner.html`) bills
exactly this, and `/fleet/:fleetId` returns the line items.

### Plus a fee per smoothed payout

On top of the seat, GigGuard takes a small fee for each payout it smooths,
**R1.50**, stubbed in `chargeForPayout` where a real build would call Paystack.
Because no driver money moves, a smoothed payout costs almost nothing to serve
(one ledger write plus one polled API read), so the fee is effectively all
margin. Every `/poll` and `/simulate/income` charges it.

### A direct consumer on ramp, the secondary line

A driver can also pay directly, which is the channel before a fleet deal exists.
This is the weaker line on purpose (the headline user is cash strapped exactly
when they need it), so it is priced to self select:

| Tier | Price | Intended buyer |
| --- | --- | --- |
| Free | R0 | Anyone trying it, one card, the cap enforced |
| Pay as you go | R1.50 per payout | A driver who only pays in weeks money actually lands, about R6 a month |
| Standard | R39 per month | A single gig regular, predictable enough to commit |
| Pro | R99 per month | A multi stream freelancer with several income sources |

## Project structure

```
GigGuard/
  card-code/
    main.js          Deployed into the Investec card IDE (before/after transaction)
  backend/
    server.js        Express API: card hooks, poll, status, fleet, onboard, billing
    package.json
    test.js          Pure logic test suite, no server needed
    data/            Persisted store (gitignored, created on first run)
  frontend/
    dashboard.html   Driver dashboard, standalone offline or live against the backend
    partner.html     Partner / fleet console, reads live from the backend
  docs/              Screenshots and demo GIFs
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

The server boots with a ready made `demo` user. The data routes need the shared
key: the same value you set as `GIGGUARD_API_KEY` in `.env`, passed as an
`x-api-key` header. So you can poke it straight away:

```
curl localhost:3000/status/demo -H 'x-api-key: your-gigguard-api-key'
```

### 2. Onboard with curl

Configure the demo user (or any user id) with your Investec sandbox details
and your two dials:

```
curl -X POST localhost:3000/setup \
  -H 'content-type: application/json' \
  -H 'x-api-key: your-gigguard-api-key' \
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
  -H 'x-api-key: your-gigguard-api-key' \
  -d '{"amountRands": 8000}'

curl localhost:3000/status/demo -H 'x-api-key: your-gigguard-api-key'
```

Pull real sandbox transactions instead of simulating:

```
curl -X POST localhost:3000/poll \
  -H 'content-type: application/json' \
  -H 'x-api-key: your-gigguard-api-key' \
  -d '{"userId":"demo","fromDate":"2026-03-01","toDate":"2026-06-01"}'
```

Try a card check the way the `beforeTransaction` hook would. The hook sends
`amount` in cents, but you can pass `amountRands` by hand. A spend over the
weekly release comes back declined:

```
curl -X POST localhost:3000/check \
  -H 'content-type: application/json' \
  -H 'x-api-key: your-gigguard-api-key' \
  -d '{"amountRands": 7467.83, "merchant": "Game"}'
```

Onboard a driver into a fleet, then read the fleet's bill (every driver plus the
per seat and per payout revenue):

```
curl -X POST localhost:3000/onboard \
  -H 'content-type: application/json' \
  -H 'x-api-key: your-gigguard-api-key' \
  -d '{"driverName": "Lerato Mthembu", "cardId": "card-lerato", "fleetId": "bolt-cpt"}'

curl localhost:3000/fleet/bolt-cpt -H 'x-api-key: your-gigguard-api-key'
```

State persists to `backend/data/store.json` (gitignored), so a restart keeps the
fleet and its ledgers. Delete that file to reseed the demo fleet from scratch.

### 3. Card IDE

In the Investec programmable banking card IDE:

1. Paste the contents of `card-code/main.js` into the editor.
2. Add two environment variables in the IDE settings:
   * `GIGGUARD_WEBHOOK_URL` set to your backend's public base URL, for example
     a tunnel like `https://your-tunnel.ngrok.app`.
   * `GIGGUARD_API_KEY` set to the same value you put in the backend `.env`.
3. Save and deploy. Now every tap of the card checks the weekly release first.

### 4. Driver dashboard

Open `frontend/dashboard.html` in a browser. With no backend it runs a built in
engine offline and the badge reads Sandbox; with the backend running it auto
detects it, flips the badge to Live, and drives the real API for income, card
checks, and settings. It is laid out like a weekly statement: one big "available
to spend this week" figure on a paper card, with the buffer, the weekly release
meter, and the runway responding as you simulate income and card taps.

A healthy week:

![GigGuard dashboard, a healthy week](docs/dashboard-overview.png)

When a spend goes over the weekly release, the card declines it and a red
notice appears:

![GigGuard declining an overspend](docs/dashboard-decline.png)

### 5. Partner console

Open `frontend/partner.html` with the backend running. This is the fleet
operator's view: every driver's buffer and weekly release, and the monthly bill
broken into per active seat and per payout smoothing fees. Onboard a driver or
simulate a payout from the page and watch the bill move. It points at
`http://localhost:3000` by default; override with `?api=` and `?key=` in the URL.

## Tests

The logic suite is pure arithmetic with no server and no network:

```
cd backend
node test.js
```

It prints a tick or a cross for each of the fourteen checks and exits non zero if
any fail, so it drops straight into CI. The checks cover zero state, the
withholding split, status readout, approvals, the decline at the boundary, a one
cent overspend, stacked income, a zero credit, a different buffer and runway
setting, a double tap that cannot both clear the weekly release, the R1.50 per
payout fee, and the per seat fleet billing.

## What it does and does not do

GigGuard is a budgeting and self control tool. Being precise about its edges is
part of the trust it asks for.

What it does:

* Skims a buffer percent off every detected income credit and releases a fixed
  weekly amount you set, so lumpy gig pay feels closer to a steady salary.
* Enforces that weekly limit at the Investec programmable card. When the backend
  answers within the card's roughly two second budget, a spend over the weekly
  release is declined at the till, not just flagged afterwards.
* Detects income by polling the Investec transactions API, runs the buffer math
  in integer cents, and keeps the weekly ledger in sync by recording only
  approved debits.

What it does not do:

* **No driver money moves.** It never moves, sweeps, transfers, holds, or
  escrows a driver's money. The buffer is a ledger, a number that says how much
  of their own income they have chosen not to spend yet; their cash stays in
  their own Investec account the whole time. The only money that changes hands is
  our own fee, per seat and per payout, and in this build even that is a stub,
  not a live charge. GigGuard is not a deposit taking, custody, or payment
  business.
* **Not a vault.** Because the funds never leave your own account, it is a self
  imposed limit on this one card, a strong nudge rather than a lock. Someone
  determined to spend the withheld money another way still can.
* **No guarantee under failure.** By deliberate design it fails open: if the
  backend is slow, unreachable, or the key is wrong, the spend is approved rather
  than stranding you at a till. Wrongly declining your only card is the worse
  harm, so the cap is a strong best effort limit, not an unbreakable one. A short
  hold on each approved spend closes the common double tap case, two taps in the
  same instant, and there is a test for it. A spend that is approved but whose
  afterTransaction never arrives can still let the weekly total drift.
* **No advice.** It does not tell anyone what to do with their money. It enforces
  a limit you set for yourself.
* **No AI.** Every decision is deterministic integer arithmetic you can read in
  `backend/server.js` and reproduce with `backend/test.js`. There is no model, no
  inference, and nothing learned from your data.

Other guardrails:

* **Integer money.** All arithmetic is in cents. No floats touch a balance.
* **Clamped settings.** Buffer percent stays within 10 to 60, runway within 1 to
  12, no matter what a client sends.
* **Sandbox only.** This repo targets the Investec sandbox. Do not point it at a
  live account.

Privacy and data:

* Per driver, GigGuard stores the Investec client id, secret, api key, and
  account id provided, plus the buffer ledger and week state. It persists to a
  gitignored JSON file (`backend/data/store.json`) so a restart does not wipe a
  ledger; a real build would use a database with the secrets encrypted at rest.
  Nothing is shared with third parties beyond the Investec API calls the product
  is built on.
* The data routes (`/setup`, `/status`, `/poll`, `/simulate/income`) require the
  shared `GIGGUARD_API_KEY`. The card hooks (`/check`, `/record`) use the same key
  but fail open, so a bad key can never brick the card. Before any non local use,
  swap the single shared key for real per user authentication, and do not expose
  this backend to the public internet as is.

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
