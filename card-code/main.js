// GigGuard card code.
//
// This is the bit that gets pasted into the Investec programmable card
// IDE. It runs on Investec's side every time the card is tapped. Two
// hooks do all the work:
//
//   beforeTransaction  decide whether to let a spend through
//   afterTransaction   tell the backend about spends the bank approved
//
// A few things about this environment that trip people up:
//   * Environment variables come from `env.NAME`, not process.env.
//   * Only `fetch` and `console.log` are really available. No npm, no
//     require, no Node built ins.
//   * Amounts here are already in cents (authorization.centsAmount).
//   * beforeTransaction has roughly a 2 second budget. If you take
//     longer, or throw, the platform approves the spend on your behalf.
//     We lean into that: anytime we are not certain, we approve, because
//     bricking someone's only card mid purchase is far worse than
//     letting one spend slip past the cap.

// beforeTransaction runs first. Return false to decline, anything else
// approves. We ask our backend whether this spend fits inside the
// week's release and pass the answer straight through.
const beforeTransaction = async (authorization) => {
  const amount = authorization.centsAmount; // already cents
  const merchant = authorization.merchant ? authorization.merchant.name : 'unknown';
  const reference = authorization.reference || '';

  try {
    const res = await fetch(`${env.GIGGUARD_WEBHOOK_URL}/check`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': env.GIGGUARD_API_KEY,
      },
      body: JSON.stringify({ amount, merchant, reference }),
    });

    // Any non 200 means the backend is unhappy or our key is wrong.
    // Fall open rather than strand the cardholder.
    if (!res.ok) {
      console.log(`GigGuard /check returned ${res.status}, approving by default`);
      return true;
    }

    const decision = await res.json();

    if (decision.approved === false) {
      console.log(`GigGuard declined R${(amount / 100).toFixed(2)} at ${merchant}: ${decision.reason}`);
      return false;
    }

    console.log(`GigGuard approved R${(amount / 100).toFixed(2)} at ${merchant}`);
    return true;
  } catch (err) {
    // Backend unreachable, DNS hiccup, timeout, malformed JSON, whatever.
    // None of that should cost the user a transaction. Approve.
    console.log(`GigGuard backend error, approving by default: ${err}`);
    return true;
  }
};

// afterTransaction runs once the spend has actually gone through. A card
// purchase is a debit, and reaching this hook means it was approved, so
// we tell the backend to add it to this week's running total. We never
// call /record from the decline path, which keeps the weekly ledger
// counting only money that truly left the card.
//
// Heads up: depending on your card settings the platform may also invoke
// a hook for declined or reversed transactions. If you wire those up,
// do not forward them here, otherwise declines would eat into the cap.
const afterTransaction = async (transaction) => {
  try {
    await fetch(`${env.GIGGUARD_WEBHOOK_URL}/record`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': env.GIGGUARD_API_KEY,
      },
      body: JSON.stringify({
        type: 'DEBIT',
        amount: transaction.centsAmount, // cents
        status: 'APPROVED',
      }),
    });
  } catch (err) {
    // If we cannot record it the worst case is the week's total drifts a
    // little low and the user gets to spend slightly more than intended.
    // Not great, not a disaster. Log it and move on.
    console.log(`GigGuard could not record spend: ${err}`);
  }
};

// Declines land here. We only log. Nothing should touch the weekly
// ledger, because no money moved. Present so the behaviour is explicit
// rather than relying on the platform never calling us.
const afterDecline = async (transaction) => {
  console.log(`GigGuard saw a decline at ${transaction && transaction.merchant ? transaction.merchant.name : 'unknown'}`);
};
