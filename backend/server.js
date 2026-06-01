// GigGuard backend.
//
// Two jobs:
//   1. Watch an Investec account for incoming money (gig payouts) and
//      skim a slice of every credit into a buffer ledger.
//   2. Answer the programmable card's beforeTransaction hook with a
//      yes or no on whether a spend fits inside this week's release.
//
// The card talks to /check and /record. A human (or a cron) hits /poll
// to pull new income. The dashboard reads /status.
//
// Nothing here moves real money. The buffer is a number we keep, not an
// account we sweep into. See the knowledge file for why that matters.

require('dotenv').config();
const express = require('express');

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;

// Investec endpoints. The token comes from their identity host, the
// account data from the sandbox host. Swap the sandbox host for the
// live one (openapi.investec.com) when you go to production.
const INVESTEC_TOKEN_URL = 'https://identity.secure.investec.com/connect/token';
const INVESTEC_API_BASE = 'https://openapisandbox.investec.com/za/pb/v1';

// ---------------------------------------------------------------------
// In memory user store. This is a demo so a Map is fine. In production
// this is a real database, because dropping someone's buffer ledger on
// a server restart would be a genuinely bad day.
// ---------------------------------------------------------------------
const users = new Map();

function clamp(n, lo, hi) {
  n = Number(n);
  if (Number.isNaN(n)) return lo;
  return Math.min(hi, Math.max(lo, n));
}

function makeUser(over = {}) {
  return {
    userId: 'demo',
    // Investec credentials, filled in by /setup
    investecClientId: '',
    investecSecret: '',
    investecApiKey: '',
    accountId: '',
    // the two dials a user actually controls
    bufferPercent: 30, // skim this much off every credit
    minWeeks: 4, // stretch the buffer over at least this many weeks
    // ledger, all in cents
    bufferBalance: 0,
    weeklyRelease: 0,
    spentThisWeek: 0,
    weekStartDate: new Date().toISOString(),
    // polling bookkeeping
    lastPollDate: null,
    seenTxns: new Set(), // keys of credits we already counted
    // short lived reservations: each approved /check holds its amount until
    // the matching /record settles or the hold expires. Stops two near
    // simultaneous taps from both reading the same "remaining" and slipping
    // past the weekly cap.
    holds: [], // [{ cents, at }]
    // cached OAuth token
    token: null,
    tokenExpiry: 0,
    ...over,
  };
}

// Seed one demo user so the card and the dashboard work the moment the
// server boots, before anyone calls /setup. If you have put Investec
// sandbox credentials in .env we pick them up here so /poll works
// straight away; otherwise fill them in later with a /setup call.
const DEMO_ID = 'demo';
users.set(DEMO_ID, makeUser({
  userId: DEMO_ID,
  investecClientId: process.env.INVESTEC_CLIENT_ID || '',
  investecSecret: process.env.INVESTEC_SECRET || '',
  investecApiKey: process.env.INVESTEC_API_KEY || '',
}));

// The card's hooks do not send us a userId (the card does not know who
// it belongs to in this demo), so we resolve to the single demo user.
// A real multi-card deployment would map the card token to a user here.
function userForCard() {
  return users.get(DEMO_ID);
}

// ---------------------------------------------------------------------
// Money. Everything internal is cents and integer math. We only render
// rands at the very edge, as strings, so nobody ever does float maths on
// someone's salary.
// ---------------------------------------------------------------------
function rands(cents) {
  return (cents / 100).toFixed(2);
}

// A credit landed. Skim the buffer percent off it, add that to the
// buffer, then recompute the weekly release off the new buffer total.
function processIncomingCredit(user, amountCents) {
  const toBuffer = Math.floor((amountCents * user.bufferPercent) / 100);
  user.bufferBalance += toBuffer;
  user.weeklyRelease = Math.floor(user.bufferBalance / user.minWeeks);
  return toBuffer;
}

// Lazy week reset. No background job. Every time we look at the card we
// check whether a full week has passed and, if so, wipe the weekly spend
// and start a new week from now.
function resetWeekIfNeeded(user) {
  const oneWeek = 7 * 24 * 60 * 60 * 1000;
  const started = new Date(user.weekStartDate).getTime();
  if (Date.now() - started >= oneWeek) {
    user.spentThisWeek = 0;
    user.weekStartDate = new Date().toISOString();
  }
}

// A check approves and reserves; the matching record settles. A reservation
// that never settles (declined after approve, dropped afterTransaction) would
// otherwise wedge the cap shut, so holds expire on their own. The window is
// the card's roughly two second decision budget plus a margin.
const HOLD_TTL_MS = 5000;

function sweepHolds(user) {
  const now = Date.now();
  user.holds = user.holds.filter((h) => now - h.at < HOLD_TTL_MS);
}

// cents currently reserved by approved-but-not-yet-settled checks
function heldCents(user) {
  sweepHolds(user);
  return user.holds.reduce((sum, h) => sum + h.cents, 0);
}

// ---------------------------------------------------------------------
// Investec API helpers.
// ---------------------------------------------------------------------

// OAuth client credentials grant. Investec wants HTTP Basic auth with
// clientId:secret base64 encoded, plus the x-api-key header, and a form
// body asking for the accounts scope. We cache the token until just
// before it expires so we are not minting a new one on every poll.
async function getInvestecToken(user) {
  if (user.token && Date.now() < user.tokenExpiry) {
    return user.token;
  }

  const basic = Buffer.from(`${user.investecClientId}:${user.investecSecret}`).toString('base64');

  const res = await fetch(INVESTEC_TOKEN_URL, {
    method: 'POST',
    headers: {
      Authorization: `Basic ${basic}`,
      'Content-Type': 'application/x-www-form-urlencoded',
      'x-api-key': user.investecApiKey,
    },
    body: 'grant_type=client_credentials&scope=accounts',
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Investec token request failed (${res.status}): ${text}`);
  }

  const data = await res.json();
  user.token = data.access_token;
  // shave 60 seconds off so we refresh before it actually lapses
  user.tokenExpiry = Date.now() + (data.expires_in - 60) * 1000;
  return user.token;
}

// Pull transactions for a date window. Investec returns amounts in
// rands as plain numbers, which is the single most important thing to
// remember in this whole codebase: the API thinks in rands, the card
// thinks in cents. We convert on the way in.
async function fetchTransactions(user, fromDate, toDate) {
  const token = await getInvestecToken(user);
  const url = `${INVESTEC_API_BASE}/accounts/${user.accountId}/transactions?fromDate=${fromDate}&toDate=${toDate}`;

  const res = await fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/json',
    },
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Investec transactions request failed (${res.status}): ${text}`);
  }

  const data = await res.json();
  return (data.data && data.data.transactions) || [];
}

function isoDate(d) {
  return d.toISOString().slice(0, 10);
}

function daysAgo(n) {
  return new Date(Date.now() - n * 24 * 60 * 60 * 1000);
}

// ---------------------------------------------------------------------
// Auth. Everything that exposes or drives a user's financial state needs the
// shared secret in x-api-key. Two flavours:
//
//   requireApiKey        for the card hooks (/check, /record). Fails OPEN with
//                        approved:true, because the card is built to never
//                        brick, so a bad key must not strand someone at a till.
//   requireApiKeyStrict  for the data routes (/setup, /status, /poll,
//                        /simulate/income). Fails CLOSED with a plain 401, so
//                        nobody can read or move a user's ledger without the key.
//
// The dashboard is fully standalone and makes no backend calls, so gating
// these routes does not affect it. Before any non local use, swap the single
// shared key for real per user auth.
// ---------------------------------------------------------------------
function requireApiKey(req, res, next) {
  const sent = req.header('x-api-key');
  if (!sent || sent !== process.env.GIGGUARD_API_KEY) {
    return res.status(401).json({ approved: true, reason: 'bad or missing api key' });
  }
  next();
}

function requireApiKeyStrict(req, res, next) {
  const sent = req.header('x-api-key');
  if (!sent || sent !== process.env.GIGGUARD_API_KEY) {
    return res.status(401).json({ error: 'unauthorized' });
  }
  next();
}

// ---------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------

// Friendly root so hitting the server in a browser is not a 404.
app.get('/', (_req, res) => {
  res.json({ service: 'gigguard', ok: true, users: users.size });
});

// Onboard a user / save their dials. Clamps keep the two settings in a
// sane range no matter what the client sends.
app.post('/setup', requireApiKeyStrict, (req, res) => {
  const {
    userId,
    investecClientId,
    investecSecret,
    investecApiKey,
    accountId,
    bufferPercent,
    minWeeks,
  } = req.body || {};

  if (!userId) {
    return res.status(400).json({ error: 'userId is required' });
  }

  const existing = users.get(userId) || makeUser({ userId });
  existing.userId = userId;
  if (investecClientId !== undefined) existing.investecClientId = investecClientId;
  if (investecSecret !== undefined) existing.investecSecret = investecSecret;
  if (investecApiKey !== undefined) existing.investecApiKey = investecApiKey;
  if (accountId !== undefined) existing.accountId = accountId;
  existing.bufferPercent = bufferPercent === undefined ? 30 : clamp(bufferPercent, 10, 60);
  existing.minWeeks = minWeeks === undefined ? 4 : clamp(minWeeks, 1, 12);
  // settings changed, so the weekly release may need recomputing
  existing.weeklyRelease = Math.floor(existing.bufferBalance / existing.minWeeks);

  users.set(userId, existing);

  res.json({
    userId: existing.userId,
    bufferPercent: existing.bufferPercent,
    minWeeks: existing.minWeeks,
    message: 'saved',
  });
});

// What the dashboard shows. Money out as rand strings, runway as a one
// decimal count of weeks the buffer would last at the current release.
app.get('/status/:userId', requireApiKeyStrict, (req, res) => {
  const user = users.get(req.params.userId);
  if (!user) {
    return res.status(404).json({ error: 'no such user' });
  }

  resetWeekIfNeeded(user);

  const remaining = user.weeklyRelease - user.spentThisWeek;
  const runway = user.weeklyRelease > 0 ? user.bufferBalance / user.weeklyRelease : 0;

  res.json({
    bufferBalance: rands(user.bufferBalance),
    weeklyRelease: rands(user.weeklyRelease),
    spentThisWeek: rands(user.spentThisWeek),
    remainingThisWeek: rands(remaining),
    runwayWeeks: runway.toFixed(1),
    bufferPercent: user.bufferPercent,
    weekStartDate: user.weekStartDate,
  });
});

// The card's beforeTransaction hook calls this. amount comes in as
// cents because the card already works in cents. We answer with a plain
// approved boolean and a human readable reason the card can log.
app.post('/check', requireApiKey, (req, res) => {
  const { amount, merchant, reference } = req.body || {};
  const user = userForCard();

  resetWeekIfNeeded(user);

  const cents = Math.round(Number(amount) || 0);
  // subtract spends already approved this instant but not yet settled, so two
  // taps in the same blink cannot both spend the last of the weekly release
  const remaining = user.weeklyRelease - user.spentThisWeek - heldCents(user);

  if (cents > remaining) {
    return res.json({
      approved: false,
      reason: `Over the weekly release. R${rands(remaining)} left, this spend is R${rands(cents)}.`,
    });
  }

  // Reserve this amount until /record settles it (or the hold expires). We do
  // not touch spentThisWeek here, that only moves on an approved, settled debit.
  user.holds.push({ cents, at: Date.now() });

  res.json({
    approved: true,
    reason: `Within the weekly release. R${rands(remaining - cents)} left after this.`,
    merchant: merchant || null,
    reference: reference || null,
  });
});

// The card's afterTransaction hook calls this once the bank has made up
// its mind. We only move the weekly ledger for an approved debit. Income
// (credits) does not touch the weekly spend, and declines obviously do
// not either.
app.post('/record', requireApiKey, (req, res) => {
  const { type, amount, status } = req.body || {};
  const user = userForCard();

  const isApprovedDebit =
    String(type).toUpperCase() === 'DEBIT' && String(status).toUpperCase() === 'APPROVED';

  if (isApprovedDebit) {
    resetWeekIfNeeded(user);
    user.spentThisWeek += Math.round(Number(amount) || 0);
    // this settled debit clears the oldest outstanding reservation
    if (user.holds.length) user.holds.shift();
  }

  res.json({
    recorded: isApprovedDebit,
    spentThisWeek: rands(user.spentThisWeek),
  });
});

// Pull new income from Investec. fromDate is wherever we last polled, or
// a week back on the first run. Every fresh credit gets run through the
// buffer engine once. We dedupe on a composite key because the sandbox
// does not hand out stable transaction ids.
app.post('/poll', requireApiKeyStrict, async (req, res) => {
  const userId = (req.body && req.body.userId) || DEMO_ID;
  const user = users.get(userId);
  if (!user) {
    return res.status(404).json({ error: 'no such user' });
  }
  if (!user.accountId || !user.investecClientId) {
    return res.status(400).json({ error: 'run /setup with Investec credentials first' });
  }

  const fromDate = user.lastPollDate || isoDate(daysAgo(7));
  const toDate = isoDate(new Date());

  let transactions;
  try {
    transactions = await fetchTransactions(user, fromDate, toDate);
  } catch (err) {
    return res.status(502).json({ error: 'could not reach Investec', detail: String(err.message) });
  }

  let creditsCounted = 0;
  let totalSkimmed = 0;

  for (const t of transactions) {
    if (String(t.type).toUpperCase() !== 'CREDIT') continue;

    // build a stable-ish key so a re-poll of the same window does not
    // count the same payout twice
    const key = `${t.transactionDate || t.postingDate || ''}|${t.description || ''}|${t.amount}`;
    if (user.seenTxns.has(key)) continue;
    user.seenTxns.add(key);

    // API gives rands, the engine wants cents
    const cents = Math.round(Number(t.amount) * 100);
    totalSkimmed += processIncomingCredit(user, cents);
    creditsCounted++;
  }

  user.lastPollDate = toDate;

  res.json({
    creditsCounted,
    skimmedToBuffer: rands(totalSkimmed),
    bufferBalance: rands(user.bufferBalance),
    weeklyRelease: rands(user.weeklyRelease),
    window: { fromDate, toDate },
  });
});

// Sandbox shortcut. Pretend a payout of amountRands just landed, without
// going anywhere near Investec. Handy for demos and for the dashboard.
app.post('/simulate/income', requireApiKeyStrict, (req, res) => {
  const userId = (req.body && req.body.userId) || DEMO_ID;
  const user = users.get(userId);
  if (!user) {
    return res.status(404).json({ error: 'no such user' });
  }

  const amountRands = Number(req.body && req.body.amountRands);
  if (!amountRands || amountRands <= 0) {
    return res.status(400).json({ error: 'amountRands must be a positive number' });
  }

  const cents = Math.round(amountRands * 100);
  const skimmed = processIncomingCredit(user, cents);

  res.json({
    incomeRands: amountRands.toFixed(2),
    skimmedToBuffer: rands(skimmed),
    spendable: rands(cents - skimmed),
    bufferBalance: rands(user.bufferBalance),
    weeklyRelease: rands(user.weeklyRelease),
  });
});

app.listen(PORT, () => {
  console.log(`GigGuard backend listening on http://localhost:${PORT}`);
  console.log(`Demo user ready. Try: curl -X POST localhost:${PORT}/simulate/income -H 'content-type: application/json' -d '{"amountRands":8000}'`);
});

// Exported for anyone who wants to unit test the pure bits without the
// HTTP layer. (test.js keeps its own copy so it can run with zero deps.)
module.exports = { processIncomingCredit, resetWeekIfNeeded, clamp, rands, makeUser };
