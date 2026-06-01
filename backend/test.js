// GigGuard logic tests. No server, no network, no Investec calls.
// Just the money math, run straight through with `node test.js`.
//
// Everything below mirrors the real functions in server.js. They are
// copied in here on purpose so the tests stay honest about the exact
// integer arithmetic the backend uses. If you change the rules in
// server.js, change them here too and watch what breaks.

// All money lives in cents. We only ever turn it into rands at the
// very edge when a human needs to read it.
function rands(cents) {
  return (cents / 100).toFixed(2);
}

function freshUser(bufferPercent, minWeeks) {
  return {
    bufferPercent: bufferPercent,
    minWeeks: minWeeks,
    bufferBalance: 0,
    weeklyRelease: 0,
    spentThisWeek: 0,
    weekStartDate: new Date().toISOString(),
    holds: [], // amounts approved but not yet settled, see canSpend/recordSpend
  };
}

// Income landed. Skim a slice into the buffer and recompute the
// weekly release off the new buffer total. Floor everywhere so we
// never invent a fraction of a cent.
function takeIncome(user, amountCents) {
  const toBuffer = Math.floor((amountCents * user.bufferPercent) / 100);
  user.bufferBalance += toBuffer;
  user.weeklyRelease = Math.floor(user.bufferBalance / user.minWeeks);
  return toBuffer;
}

// Lazy week reset. We do not run a cron. Whenever someone touches the
// card we just check if a week has gone by and wipe the weekly spend.
function resetWeekIfNeeded(user) {
  const oneWeek = 7 * 24 * 60 * 60 * 1000;
  const started = new Date(user.weekStartDate).getTime();
  if (Date.now() - started >= oneWeek) {
    user.spentThisWeek = 0;
    user.weekStartDate = new Date().toISOString();
  }
}

// Reservations. An approved check holds its amount until the matching record
// settles or the hold ages out, so two taps in the same blink cannot both
// spend the last of the release. Holds expire after the card's roughly two
// second budget plus margin.
const holdTtlMs = 5000;

function heldCents(user) {
  const now = Date.now();
  user.holds = user.holds.filter((h) => now - h.at < holdTtlMs);
  return user.holds.reduce((sum, h) => sum + h.cents, 0);
}

// The actual gatekeeper. This is what the card's beforeTransaction
// hook leans on. If the spend would push you past the weekly release
// (counting amounts already reserved this instant), it gets turned away.
// No overdraft, no "just this once".
function canSpend(user, amountCents) {
  resetWeekIfNeeded(user);
  const remaining = user.weeklyRelease - user.spentThisWeek - heldCents(user);
  if (amountCents > remaining) {
    return {
      approved: false,
      remaining: remaining,
      reason: `Declined: only R${rands(remaining)} left in this week's release (you tried R${rands(amountCents)}).`,
    };
  }
  user.holds.push({ cents: amountCents, at: Date.now() });
  return {
    approved: true,
    remaining: remaining,
    reason: `Approved: R${rands(remaining - amountCents)} left this week after this one.`,
  };
}

function recordSpend(user, amountCents) {
  user.spentThisWeek += amountCents;
  // release the reservation this debit settles, matched by amount
  const i = user.holds.findIndex((h) => h.cents === amountCents);
  if (i !== -1) user.holds.splice(i, 1);
  else if (user.holds.length) user.holds.shift();
}

// Billing, the only place money actually changes hands. Same numbers as the
// backend: a R1.50 stubbed fee per smoothed payout, and R25 per active driver
// per month that the fleet pays.
const payoutFeeCents = 150;
const seatPriceCents = 2500;

function chargeForPayout(user) {
  user.payoutsSmoothed = (user.payoutsSmoothed || 0) + 1;
  user.revenueCents = (user.revenueCents || 0) + payoutFeeCents;
  return payoutFeeCents;
}

function fleetBilling(drivers) {
  const active = drivers.filter((u) => u.bufferBalance > 0);
  const seatRevenue = active.length * seatPriceCents;
  const smoothingRevenue = drivers.reduce((s, u) => s + (u.revenueCents || 0), 0);
  return { activeDrivers: active.length, seatRevenue, smoothingRevenue, total: seatRevenue + smoothingRevenue };
}

// What the dashboard / status endpoint reports. Money fields come out
// as rand strings, runway as a one decimal week count.
function statusOf(user) {
  const remaining = user.weeklyRelease - user.spentThisWeek;
  const runway = user.weeklyRelease > 0 ? user.bufferBalance / user.weeklyRelease : 0;
  return {
    bufferBalance: rands(user.bufferBalance),
    weeklyRelease: rands(user.weeklyRelease),
    spentThisWeek: rands(user.spentThisWeek),
    remainingThisWeek: rands(remaining),
    runwayWeeks: runway.toFixed(1),
    bufferPercent: user.bufferPercent,
  };
}

// ---- tiny test runner ----------------------------------------------

let passed = 0;
let failed = 0;

function expect(label, condition, detail) {
  const mark = condition ? '✓' : '✗';
  let line = `${mark} ${label}`;
  if (detail) line += `   ${detail}`;
  console.log(line);
  if (condition) passed++;
  else failed++;
}

console.log('GigGuard logic tests\n');

// 1. Zero state. A brand new user owes nothing and can spend nothing.
const u = freshUser(30, 4);
expect(
  '1  zero state on a fresh user',
  u.bufferBalance === 0 && u.weeklyRelease === 0 && u.spentThisWeek === 0,
  `buffer=${rands(u.bufferBalance)} release=${rands(u.weeklyRelease)} spent=${rands(u.spentThisWeek)}`
);

// 2. R8,000 lands at a 30% buffer.
const skim = takeIncome(u, 800000);
const spendable = 800000 - skim;
expect(
  '2  R8,000 income at 30% buffer',
  skim === 240000 && spendable === 560000 && u.weeklyRelease === 60000 && u.bufferBalance === 240000,
  `toBuffer=R${rands(skim)} spendable=R${rands(spendable)} release=R${rands(u.weeklyRelease)} buffer=R${rands(u.bufferBalance)}`
);

// 3. Status after that first paycheck. Runway should read four weeks.
const s = statusOf(u);
expect(
  '3  status reads back correctly, runway 4.0 weeks',
  s.bufferBalance === '2400.00' &&
    s.weeklyRelease === '600.00' &&
    s.spentThisWeek === '0.00' &&
    s.remainingThisWeek === '600.00' &&
    s.runwayWeeks === '4.0',
  `runway=${s.runwayWeeks} remaining=R${s.remainingThisWeek}`
);

// 4. A R200 tap goes through and the week ledger moves.
const r4 = canSpend(u, 20000);
if (r4.approved) recordSpend(u, 20000);
expect(
  '4  R200 spend approved, R400 left',
  r4.approved === true && u.spentThisWeek === 20000 && u.weeklyRelease - u.spentThisWeek === 40000,
  `spent=R${rands(u.spentThisWeek)} remaining=R${rands(u.weeklyRelease - u.spentThisWeek)}`
);

// 5. R500 is more than the R400 left, so the card says no.
const r5 = canSpend(u, 50000);
expect(
  '5  R500 spend declined, reason names the R400.00 left',
  r5.approved === false && r5.reason.includes('R400.00'),
  r5.reason
);

// 6. Spending exactly what is left is fine. Lands you on zero.
const r6 = canSpend(u, 40000);
if (r6.approved) recordSpend(u, 40000);
expect(
  '6  exactly R400 approved at the boundary, R0 left',
  r6.approved === true && u.weeklyRelease - u.spentThisWeek === 0,
  `remaining=R${rands(u.weeklyRelease - u.spentThisWeek)}`
);

// 7. One cent over zero is still over. Declined.
const r7 = canSpend(u, 1);
expect(
  '7  R0.01 over an empty week declined',
  r7.approved === false,
  r7.reason
);

// 8. Second paycheck of R3,000 stacks onto the buffer.
takeIncome(u, 300000);
expect(
  '8  second income of R3,000 lifts buffer to R3,300, release to R825',
  u.bufferBalance === 330000 && u.weeklyRelease === 82500,
  `buffer=R${rands(u.bufferBalance)} release=R${rands(u.weeklyRelease)}`
);

// 9. A zero credit (it happens with reversals) must not move anything.
const beforeZero = u.bufferBalance;
takeIncome(u, 0);
expect(
  '9  zero income leaves the buffer untouched',
  u.bufferBalance === beforeZero && u.bufferBalance === 330000,
  `buffer=R${rands(u.bufferBalance)}`
);

// 10. Different dials: a saver on 50% over an 8 week runway.
const saver = freshUser(50, 8);
const skim10 = takeIncome(saver, 1000000);
expect(
  '10 50% buffer over 8 weeks: R10,000 income skims R5,000, releases R625',
  skim10 === 500000 && saver.weeklyRelease === 62500,
  `toBuffer=R${rands(skim10)} release=R${rands(saver.weeklyRelease)}`
);

// 11. Double tap. Two R500 checks land before either settles. With a R600
// weekly release the first holds R500, leaving R100, so the second is turned
// away. Without the hold both would read R600 free and slip past the cap.
const dt = freshUser(30, 4);
takeIncome(dt, 800000); // release R600
const tapA = canSpend(dt, 50000);
const tapB = canSpend(dt, 50000);
expect(
  '11 two taps in the same blink cannot both clear the release',
  tapA.approved === true && tapB.approved === false,
  `first approved, second saw only R${rands(tapB.remaining)} free`
);

// 12. The payment stub takes R1.50 per smoothed payout. That is our revenue.
const earner = freshUser(30, 4);
takeIncome(earner, 700000); chargeForPayout(earner);
takeIncome(earner, 60000); chargeForPayout(earner);
expect(
  '12 paystack stub charges R1.50 per smoothed payout',
  earner.revenueCents === 300 && earner.payoutsSmoothed === 2,
  `R${rands(earner.revenueCents)} over ${earner.payoutsSmoothed} payouts`
);

// 13 and 14. A fleet of four drivers, three earning and one idle. The partner
// pays R25 per active seat plus the smoothing fees.
const dA = freshUser(30, 4); takeIncome(dA, 700000); chargeForPayout(dA);
const dB = freshUser(30, 4); takeIncome(dB, 500000); chargeForPayout(dB);
const dC = freshUser(30, 4); takeIncome(dC, 900000); chargeForPayout(dC);
const dIdle = freshUser(30, 4); // never earned, buffer 0, not an active seat
const bill = fleetBilling([dA, dB, dC, dIdle]);
expect(
  '13 fleet bills R25 per active seat, three of four active',
  bill.activeDrivers === 3 && bill.seatRevenue === 7500,
  `active=${bill.activeDrivers} seats=R${rands(bill.seatRevenue)}`
);
expect(
  '14 fleet revenue is seats plus the per payout smoothing fees',
  bill.smoothingRevenue === 450 && bill.total === 7950,
  `smoothing=R${rands(bill.smoothingRevenue)} total=R${rands(bill.total)}`
);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
