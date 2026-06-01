// GigGuard backend.
//
// What it does:
//   1. Watches an Investec account for incoming money (gig payouts) and skims a
//      slice of every credit into a per driver buffer ledger.
//   2. Answers the programmable card's beforeTransaction hook with a yes or no
//      on whether a spend fits inside this week's release.
//   3. Runs as a fleet product: many drivers under a partner (a fleet operator
//      or aggregator), with per seat billing and a stubbed per payout fee, so
//      the business model is something you can actually poke, not just prose.
//
// The card talks to /check and /record. A human or a cron hits /poll to pull
// new income. The consumer dashboard reads /status. The partner dashboard reads
// /fleet/:fleetId. State persists to a JSON file so a restart does not wipe a
// driver's ledger.
//
// Nothing here moves a driver's own money. The buffer is a number we keep, not
// an account we sweep into. The only money that changes hands is our own fee.

require('dotenv').config();
const express = require('express');
const fs = require('fs');
const path = require('path');

const app = express();
app.use(express.json());

// Let the dashboards (opened from a file or another origin) call the API.
// Wide open is fine for a sandbox demo; lock this down before any real use.
app.use((req, res, next) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Headers', 'content-type, x-api-key');
  res.header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  if (req.method === 'OPTIONS') return res.sendStatus(204);
  next();
});

const port = process.env.PORT || 3000;

// Investec endpoints. In the sandbox the token and the data both come from the
// openapisandbox host, and the token path is /identity/v2/oauth2/token. This
// trips people up: the live token endpoint is a different host entirely
// (identity.secure.investec.com/connect/token), so sandbox code pointed at the
// live host gets a token error and never reaches the data. Override via env for
// production.
const investecTokenUrl =
  process.env.INVESTEC_TOKEN_URL || 'https://openapisandbox.investec.com/identity/v2/oauth2/token';
const investecApiBase =
  process.env.INVESTEC_API_BASE || 'https://openapisandbox.investec.com/za/pb/v1';

// What we would charge. The R1.50 per payout is a stubbed Paystack fee; the
// R25 per active driver per month is the per seat price a fleet pays.
const payoutFeeCents = 150;
const seatPriceCents = 2500;

// ---------------------------------------------------------------------
// Store. A Map in memory, mirrored to a JSON file so a restart does not lose a
// driver's ledger. In production this is a real database with the secrets
// encrypted at rest; the file is the demo's stand in for that.
// ---------------------------------------------------------------------
const users = new Map();
const storePath = process.env.STORE_PATH || path.join(__dirname, 'data', 'store.json');

function saveStore() {
  try {
    fs.mkdirSync(path.dirname(storePath), { recursive: true });
    const arr = [...users.values()].map((u) => ({ ...u, seenTxns: [...u.seenTxns] }));
    fs.writeFileSync(storePath, JSON.stringify({ users: arr }, null, 2));
  } catch (e) {
    console.log('could not save store:', e.message);
  }
}

function loadStore() {
  try {
    const obj = JSON.parse(fs.readFileSync(storePath, 'utf8'));
    if (!obj || !Array.isArray(obj.users)) return false;
    users.clear();
    for (const u of obj.users) {
      u.seenTxns = new Set(u.seenTxns || []);
      u.holds = u.holds || [];
      users.set(u.userId, u);
    }
    return users.size > 0;
  } catch {
    return false;
  }
}

function clamp(n, lo, hi) {
  n = Number(n);
  if (Number.isNaN(n)) return lo;
  return Math.min(hi, Math.max(lo, n));
}

function makeUser(over = {}) {
  return {
    userId: 'demo',
    // who and where: a driver belongs to a fleet (the paying partner)
    driverName: '',
    fleetId: '',
    fleetName: '',
    cardId: '', // the programmable card this driver carries, maps a tap to a user
    // Investec credentials, filled in by /setup or /onboard
    investecClientId: '',
    investecSecret: '',
    investecApiKey: '',
    accountId: '',
    // the two dials a driver controls
    bufferPercent: 30, // skim this much off every credit
    minWeeks: 4, // stretch the buffer over at least this many weeks
    // ledger, all in cents
    bufferBalance: 0,
    weeklyRelease: 0,
    spentThisWeek: 0,
    weekStartDate: new Date().toISOString(),
    // what we have billed this driver: a R1.50 fee per smoothed payout
    payoutsSmoothed: 0,
    revenueCents: 0,
    // polling bookkeeping
    lastPollDate: null,
    seenTxns: new Set(), // keys of credits we already counted
    // short lived reservations: each approved /check holds its amount until the
    // matching /record settles or the hold expires, so two near simultaneous
    // taps cannot both read the same "remaining" and slip past the weekly cap.
    holds: [], // [{ cents, at }]
    // cached OAuth token
    token: null,
    tokenExpiry: 0,
    ...over,
  };
}

const demoId = 'demo';

// First boot with no store: seed a demo fleet so both dashboards have something
// to show. The demo driver also carries the Investec sandbox creds (from .env)
// so the live /poll works; the rest are simulated.
function seedFleet() {
  const fleetId = 'bolt-cpt';
  const fleetName = 'Bolt Cape Town';

  users.set(demoId, makeUser({
    userId: demoId,
    driverName: 'Demo driver (you)',
    fleetId,
    fleetName,
    cardId: 'card-demo',
    investecClientId: process.env.INVESTEC_CLIENT_ID || '',
    investecSecret: process.env.INVESTEC_SECRET || '',
    investecApiKey: process.env.INVESTEC_API_KEY || '',
    accountId: process.env.INVESTEC_ACCOUNT_ID || '',
  }));

  // a handful of drivers with lumpy, feast or famine income already smoothed
  const seedDrivers = [
    { userId: 'thabo', driverName: 'Thabo Mokoena', cardId: 'card-thabo', incomes: [700000, 60000] },
    { userId: 'naledi', driverName: 'Naledi Khumalo', cardId: 'card-naledi', incomes: [500000, 150000, 90000] },
    { userId: 'sipho', driverName: 'Sipho Dlamini', cardId: 'card-sipho', incomes: [900000, 200000] },
    { userId: 'aisha', driverName: 'Aisha Patel', cardId: 'card-aisha', incomes: [300000] },
  ];
  for (const d of seedDrivers) {
    const u = makeUser({ userId: d.userId, driverName: d.driverName, fleetId, fleetName, cardId: d.cardId });
    for (const cents of d.incomes) {
      processIncomingCredit(u, cents);
      chargeForPayout(u);
    }
    users.set(d.userId, u);
  }
}

// Map a card tap to its driver. The card sends its own id (authorization.card.id)
// so one backend serves a whole fleet. Falls back to the demo driver if a tap
// arrives with no known card, so the simplest single card setup still works.
function userForCard(cardId) {
  if (cardId) {
    for (const u of users.values()) if (u.cardId === cardId) return u;
  }
  return users.get(demoId);
}

// ---------------------------------------------------------------------
// Money. Everything internal is cents and integer math. We only render rands at
// the very edge, as strings, so nobody ever does float maths on a salary.
// ---------------------------------------------------------------------
function rands(cents) {
  return (cents / 100).toFixed(2);
}

// A credit landed. Skim the buffer percent off it, add that to the buffer, then
// recompute the weekly release off the new buffer total.
function processIncomingCredit(user, amountCents) {
  const toBuffer = Math.floor((amountCents * user.bufferPercent) / 100);
  user.bufferBalance += toBuffer;
  user.weeklyRelease = Math.floor(user.bufferBalance / user.minWeeks);
  return toBuffer;
}

// The one place money actually changes hands: our fee. Stubbed where a real
// build would call Paystack or Stripe. We just record the R1.50 we would take
// for smoothing one payout, so the revenue line is real, not a slide.
function chargeForPayout(user) {
  user.payoutsSmoothed = (user.payoutsSmoothed || 0) + 1;
  user.revenueCents = (user.revenueCents || 0) + payoutFeeCents;
  console.log(`[paystack stub] charge R${rands(payoutFeeCents)} to smooth a payout for ${user.driverName || user.userId} (revenue to date R${rands(user.revenueCents)})`);
  return payoutFeeCents;
}

// Lazy week reset. No background job. Every time we look at the card we check
// whether a full week has passed and, if so, wipe the weekly spend.
function resetWeekIfNeeded(user) {
  const oneWeek = 7 * 24 * 60 * 60 * 1000;
  const started = new Date(user.weekStartDate).getTime();
  if (Date.now() - started >= oneWeek) {
    user.spentThisWeek = 0;
    user.weekStartDate = new Date().toISOString();
  }
}

// A check approves and reserves; the matching record settles. A reservation
// that never settles would wedge the cap shut, so holds expire on their own.
// The window is the card's roughly two second budget plus a margin.
const holdTtlMs = 5000;

function sweepHolds(user) {
  const now = Date.now();
  user.holds = user.holds.filter((h) => now - h.at < holdTtlMs);
}

function heldCents(user) {
  sweepHolds(user);
  return user.holds.reduce((sum, h) => sum + h.cents, 0);
}

// Roll a fleet up into the numbers a partner cares about: each driver's buffer
// and what we are billing them for it.
function fleetSummary(fleetId) {
  const drivers = [...users.values()].filter((u) => u.fleetId === fleetId);
  const active = drivers.filter((u) => u.bufferBalance > 0);
  const seatRevenue = active.length * seatPriceCents;
  const smoothingRevenue = drivers.reduce((s, u) => s + (u.revenueCents || 0), 0);
  return {
    fleetId,
    fleetName: (drivers[0] && drivers[0].fleetName) || fleetId,
    driverCount: drivers.length,
    activeDrivers: active.length,
    drivers: drivers.map((u) => {
      const remaining = u.weeklyRelease - u.spentThisWeek;
      const runway = u.weeklyRelease > 0 ? u.bufferBalance / u.weeklyRelease : 0;
      return {
        userId: u.userId,
        driverName: u.driverName || u.userId,
        bufferBalance: rands(u.bufferBalance),
        weeklyRelease: rands(u.weeklyRelease),
        spentThisWeek: rands(u.spentThisWeek),
        remainingThisWeek: rands(remaining),
        runwayWeeks: runway.toFixed(1),
        payoutsSmoothed: u.payoutsSmoothed || 0,
        active: u.bufferBalance > 0,
      };
    }),
    billing: {
      seatPriceRands: rands(seatPriceCents),
      activeDrivers: active.length,
      seatRevenueRands: rands(seatRevenue),
      smoothingRevenueRands: rands(smoothingRevenue),
      monthlyTotalRands: rands(seatRevenue + smoothingRevenue),
    },
  };
}

// ---------------------------------------------------------------------
// Investec API helpers.
// ---------------------------------------------------------------------

// OAuth client credentials grant. Investec wants HTTP Basic auth with
// clientId:secret base64 encoded, plus the x-api-key header, and a form body
// asking for the accounts scope. We cache the token until just before it
// expires so we are not minting a new one on every poll.
async function getInvestecToken(user) {
  if (user.token && Date.now() < user.tokenExpiry) {
    return user.token;
  }

  const basic = Buffer.from(`${user.investecClientId}:${user.investecSecret}`).toString('base64');

  const res = await fetch(investecTokenUrl, {
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

// Pull transactions for a date window. Investec returns amounts in rands as
// plain numbers: the API thinks in rands, the card thinks in cents. We convert
// on the way in.
async function fetchTransactions(user, fromDate, toDate) {
  const token = await getInvestecToken(user);
  const url = `${investecApiBase}/accounts/${user.accountId}/transactions?fromDate=${fromDate}&toDate=${toDate}`;

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
// Auth. Everything that exposes or drives money state needs the shared secret
// in x-api-key. Two flavours:
//   requireApiKey        for the card hooks (/check, /record). Fails OPEN with
//                        approved:true, so a bad key never bricks the card.
//   requireApiKeyStrict  for the data routes. Fails CLOSED with a plain 401, so
//                        nobody reads or moves a ledger without the key.
// Before any non local use, swap the single shared key for real per user auth.
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

app.get('/', (_req, res) => {
  res.json({ service: 'gigguard', ok: true, drivers: users.size });
});

// Onboard a driver. This is the partner facing "add a driver" call, the stub
// for what would be a real connect flow. Idempotent on userId / name slug.
app.post('/onboard', requireApiKeyStrict, (req, res) => {
  const { driverName, userId, fleetId, fleetName, cardId, bufferPercent, minWeeks } = req.body || {};
  if (!driverName && !userId) {
    return res.status(400).json({ error: 'driverName or userId is required' });
  }
  const slug = (userId || driverName).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
  const id = slug || `driver-${users.size + 1}`;

  const u = users.get(id) || makeUser({ userId: id });
  u.userId = id;
  if (driverName) u.driverName = driverName;
  u.fleetId = fleetId || u.fleetId || 'bolt-cpt';
  u.fleetName = fleetName || u.fleetName || 'Bolt Cape Town';
  if (cardId) u.cardId = cardId;
  u.bufferPercent = bufferPercent === undefined ? (u.bufferPercent || 30) : clamp(bufferPercent, 10, 60);
  u.minWeeks = minWeeks === undefined ? (u.minWeeks || 4) : clamp(minWeeks, 1, 12);
  users.set(id, u);
  saveStore();

  res.json({ userId: id, driverName: u.driverName, fleetId: u.fleetId, cardId: u.cardId, message: 'driver onboarded' });
});

// Save a driver's Investec credentials and dials.
app.post('/setup', requireApiKeyStrict, (req, res) => {
  const { userId, investecClientId, investecSecret, investecApiKey, accountId, bufferPercent, minWeeks } = req.body || {};
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
  existing.weeklyRelease = Math.floor(existing.bufferBalance / existing.minWeeks);

  users.set(userId, existing);
  saveStore();

  res.json({ userId: existing.userId, bufferPercent: existing.bufferPercent, minWeeks: existing.minWeeks, message: 'saved' });
});

// What a driver's own dashboard shows.
app.get('/status/:userId', requireApiKeyStrict, (req, res) => {
  const user = users.get(req.params.userId);
  if (!user) {
    return res.status(404).json({ error: 'no such user' });
  }

  resetWeekIfNeeded(user);

  const remaining = user.weeklyRelease - user.spentThisWeek;
  const runway = user.weeklyRelease > 0 ? user.bufferBalance / user.weeklyRelease : 0;

  res.json({
    userId: user.userId,
    driverName: user.driverName || null,
    fleetName: user.fleetName || null,
    bufferBalance: rands(user.bufferBalance),
    weeklyRelease: rands(user.weeklyRelease),
    spentThisWeek: rands(user.spentThisWeek),
    remainingThisWeek: rands(remaining),
    runwayWeeks: runway.toFixed(1),
    bufferPercent: user.bufferPercent,
    minWeeks: user.minWeeks,
    weekStartDate: user.weekStartDate,
  });
});

// What a partner (fleet operator) sees: every driver plus the bill.
app.get('/fleet/:fleetId', requireApiKeyStrict, (req, res) => {
  res.json(fleetSummary(req.params.fleetId));
});

// The card's beforeTransaction hook. amount comes in as cents (the card works
// in cents); amountRands is accepted too for hand testing. cardId maps the tap
// to its driver.
app.post('/check', requireApiKey, (req, res) => {
  const { amount, amountRands, merchant, reference, cardId } = req.body || {};
  const user = userForCard(cardId);

  resetWeekIfNeeded(user);

  const cents = amount != null
    ? Math.round(Number(amount) || 0)
    : Math.round((Number(amountRands) || 0) * 100);
  // subtract spends approved this instant but not yet settled, so two taps in
  // the same blink cannot both spend the last of the weekly release
  const remaining = user.weeklyRelease - user.spentThisWeek - heldCents(user);

  console.log(`/check ${user.driverName || user.userId}: ${merchant || 'card'} wants R${rands(cents)}, R${rands(remaining)} left this week`);

  if (cents > remaining) {
    return res.json({
      approved: false,
      reason: `Over the weekly release. R${rands(remaining)} left, this spend is R${rands(cents)}.`,
    });
  }

  user.holds.push({ cents, at: Date.now() });

  res.json({
    approved: true,
    reason: `Within the weekly release. R${rands(remaining - cents)} left after this.`,
    merchant: merchant || null,
    reference: reference || null,
  });
});

// The card's afterTransaction hook. We only move the weekly ledger for an
// approved debit.
app.post('/record', requireApiKey, (req, res) => {
  const { type, amount, status, cardId } = req.body || {};
  const user = userForCard(cardId);

  const isApprovedDebit =
    String(type).toUpperCase() === 'DEBIT' && String(status).toUpperCase() === 'APPROVED';

  if (isApprovedDebit) {
    resetWeekIfNeeded(user);
    const cents = Math.round(Number(amount) || 0);
    user.spentThisWeek += cents;
    // release the reservation this debit settles, matched by amount, falling
    // back to the oldest hold; the TTL sweep mops up anything left.
    const i = user.holds.findIndex((h) => h.cents === cents);
    if (i !== -1) user.holds.splice(i, 1);
    else if (user.holds.length) user.holds.shift();
    saveStore();
  }

  res.json({ recorded: isApprovedDebit, spentThisWeek: rands(user.spentThisWeek) });
});

// Pull new income from Investec and charge our fee for each payout smoothed.
app.post('/poll', requireApiKeyStrict, async (req, res) => {
  const userId = (req.body && req.body.userId) || demoId;
  const user = users.get(userId);
  if (!user) {
    return res.status(404).json({ error: 'no such user' });
  }
  if (!user.accountId || !user.investecClientId) {
    return res.status(400).json({ error: 'run /setup with Investec credentials first' });
  }

  const fromDate = (req.body && req.body.fromDate) || user.lastPollDate || isoDate(daysAgo(7));
  const toDate = (req.body && req.body.toDate) || isoDate(new Date());

  let transactions;
  try {
    transactions = await fetchTransactions(user, fromDate, toDate);
  } catch (err) {
    return res.status(502).json({ error: 'could not reach Investec', detail: String(err.message) });
  }

  let creditsCounted = 0;
  let totalSkimmed = 0;
  let feesCharged = 0;

  for (const t of transactions) {
    if (String(t.type).toUpperCase() !== 'CREDIT') continue;

    // prefer a real id; otherwise include runningBalance so two identical same
    // day credits do not collapse into one
    const key = t.uuid || `${t.transactionDate || t.postingDate || ''}|${t.description || ''}|${t.amount}|${t.runningBalance ?? ''}`;
    if (user.seenTxns.has(key)) continue;
    user.seenTxns.add(key);

    const cents = Math.round(Number(t.amount) * 100);
    totalSkimmed += processIncomingCredit(user, cents);
    feesCharged += chargeForPayout(user);
    creditsCounted++;
  }

  user.lastPollDate = toDate;
  saveStore();

  res.json({
    creditsCounted,
    skimmedToBuffer: rands(totalSkimmed),
    bufferBalance: rands(user.bufferBalance),
    weeklyRelease: rands(user.weeklyRelease),
    feesCharged: rands(feesCharged),
    revenueToDate: rands(user.revenueCents),
    window: { fromDate, toDate },
  });
});

// Sandbox shortcut. Pretend a payout of amountRands landed, charge the fee, no
// Investec call. Handy for demos and for the dashboard's live mode.
app.post('/simulate/income', requireApiKeyStrict, (req, res) => {
  const userId = (req.body && req.body.userId) || demoId;
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
  const fee = chargeForPayout(user);
  saveStore();

  res.json({
    incomeRands: amountRands.toFixed(2),
    skimmedToBuffer: rands(skimmed),
    spendable: rands(cents - skimmed),
    bufferBalance: rands(user.bufferBalance),
    weeklyRelease: rands(user.weeklyRelease),
    feeCharged: rands(fee),
    revenueToDate: rands(user.revenueCents),
  });
});

// boot: load the saved store, or seed a demo fleet on the very first run
if (!loadStore()) {
  seedFleet();
  saveStore();
}

app.listen(port, () => {
  console.log(`GigGuard backend listening on http://localhost:${port}`);
  console.log(`${users.size} drivers loaded. Fleet view: curl localhost:${port}/fleet/bolt-cpt -H 'x-api-key: <key>'`);
});

// Exported for the test suite to exercise the pure bits without the HTTP layer.
module.exports = { processIncomingCredit, resetWeekIfNeeded, chargeForPayout, fleetSummary, clamp, rands, makeUser, payoutFeeCents, seatPriceCents };
