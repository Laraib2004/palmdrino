# Design decisions

Records the choices this implementation makes, why, and what is still open.
Answers the open questions in §8 of `palm-pay-project.md` and corrects two
points in that document that would not survive contact with the regulations.

---

## D1. Matching architecture: 1:small-N (resolves §6.2 and §8.2)

**Decision:** the customer supplies a short identifier at the till, which
selects a *shard*; the palm is then matched only against the palms in that
shard.

`§6.2` calls the conflict between per-customer encryption and fast 1:N search
"the single most important technical decision in the project", and it is. Three
options were on the table:

| Option | Privacy | Accuracy | Verdict |
|---|---|---|---|
| Pure 1:N over a shared-key index | One key protects every template | Poor: false-match risk compounds over the whole database | Rejected |
| Pure 1:N inside a TEE | Strong | Same accuracy problem | Deferred: needs hardware, and does not fix accuracy |
| 1:small-N with an identifier hint | Per-customer DEK preserved intact | Bounded comparisons | **Chosen** |

Why the accuracy argument decides it: a false accept is not a fixed
probability, it accumulates with every comparison performed. At a
single-comparison FAR of 1e-3, a shard of 8 gives roughly a 1-in-450 chance of
charging the wrong person per transaction; a true 1:N search over a million
enrolled palms is hopeless at any threshold a real biometric can reach. Search
narrowing is not a UX compromise, it is what makes the false-accept budget
survivable. `backend/scripts/benchmark.py` prints this curve.

The privacy property is fully preserved: each candidate's DEK is unwrapped
individually to decrypt only that candidate's template
(`backend/palmpay/services/payment.py`). There is no shared template-index key and no
plaintext template store anywhere.

**Cost, stated honestly:** identification performs O(shard size) unwrap and
decrypt operations. `max_candidates` caps it, and a shard exceeding the cap is
*refused* rather than truncated — a hint that has stopped narrowing is an
accuracy regression, and silently matching the first N would hide it behind a
plausible-looking success.

**What was given up:** the pure walk-up-and-scan magic. The customer types four
digits. See D2 for why that turned out to be a benefit rather than a cost.

---

## D2. Correction: a palm alone is not Strong Customer Authentication

**`palm-pay-project.md` §5 says:** *"The palm scan is the SCA inherence factor.
It replaces the PIN"*, and concludes that any amount can therefore be charged
PIN-free.

**The first half is right. The conclusion does not follow.**

PSD2 (Art. 4(30) and the RTS on SCA) defines strong customer authentication as
two or more elements drawn from *different* categories — knowledge, possession,
inherence. Card + PIN qualifies: possession (the card) plus knowledge (the
PIN). A palm on its own is one element from one category. It is not SCA, no
matter how good the biometric is, and no acquirer can decide otherwise — this
is regulation, not risk appetite.

This matters because the entire "any amount, no PIN" claim rests on the
transaction being strongly authenticated. With inherence only, it is not, and
large amounts fall back on the low-value exemption, which caps at ~€50 per
transaction and ~€150 cumulative.

**The fix falls out of D1 at no extra cost.** The identifier hint needed for
1:small-N can double as the second factor — but only if it is a customer-chosen
secret rather than a public identifier:

| Hint | Category | Result |
|---|---|---|
| Phone last-4, member number | none — it identifies, it does not authenticate | Inherence only. Low-value exemption only. |
| Customer-chosen secret code | knowledge | **Two categories. Properly authenticated at any amount.** |

Both are implemented and the distinction is enforced, not assumed: `hint_type`
is recorded per profile, and `hint_factor()` returns a knowledge factor only
for `SECRET` (`backend/palmpay/payments/sca.py`). The API defaults to `secret` and the
Android app only offers that path.

So the outcome §5 wanted is achieved — any amount, no PIN — but by a route that
actually satisfies PSD2. The four digits the customer types are what make it
legal, not a concession.

**Also enforced:** a failed liveness check voids the inherence factor entirely.
A spoofed palm is not a weak authentication element, it is not one at all.

---

## D3. Correction confirmed: crypto-shredding, not "a private key"

`§1.2` of the design already corrects this and the correction is right, so this
note only records that the implementation follows it exactly:

- per-customer AES-256 **DEK** encrypts template, payment token and PII;
- the DEK is wrapped by a **KEK** that never leaves the KMS;
- erasure destroys the wrapped DEK, orphaning every ciphertext under it,
  including copies already written to backups.

Two hardening details worth noting, both implemented:

- **Wrapped DEKs are bound to their owner** via AEAD additional data. Without
  this, anyone with database write access could copy victim A's ciphertext onto
  victim B's row and read it back through a benign code path. With it, the
  unwrap simply fails.
- **Field ciphertexts are bound to `(customer, field)`** for the same reason —
  it blocks moving the payment-token blob into the email field to have it
  returned in cleartext.

---

## D4. Modality: palm-print now, vein-ready by construction (resolves §8.1)

Palm-print over RGB, because it can be built and benchmarked today against
public datasets and works with the phone camera the Android app already has.

The NIR path is a seam, not a promise. `backend/palmpay/palmprint/registry.py`
declares `PALM_VEIN_NIR` and raises `NotImplementedError` listing exactly what
must be supplied. Every layer above resolves its engine through `get_engine()`
and never imports a concrete extractor, so adding vein means writing three
classes and one registry entry.

Templates carry an `engine_id` and the matcher **refuses** to compare across
engines. A modality migration is therefore an explicit re-enrollment rather
than a silent accuracy collapse.

Note when that day comes: **the palm-print threshold does not transfer.** A new
FAR/FRR sweep is mandatory.

---

## D5. Payments: one gateway abstraction, Nexi mocked

Nexi acquires both Visa and Mastercard through the same tokenisation and
authorisation path, so **card scheme is metadata, never a branch**. It is used
for display and for scheme-specific stored-credential rules, and that is all.
`test_payments.py::test_visa_and_mastercard_take_the_same_path` locks this in.

`MockNexiGateway` implements the `PaymentGateway` protocol with dummy balances,
scripted declines, and idempotency. Swapping in a real Nexi adapter is a
one-line change in `backend/palmpay/services/container.py`.

The real adapter's contract is documented in `backend/palmpay/payments/nexi_mock.py`
but deliberately **not guessed at** — endpoint shapes must come from Nexi's own
documentation. §8.3 (which Nexi product supports biometric-triggered
card-on-file charges) remains open and is a commercial question, not a coding
one.

Two things carried through that are easy to get wrong:

- **Money is integer minor units everywhere.** `0.1 + 0.2 != 0.3` in binary
  floating point, and rounding drift in an authorisation amount is a
  reconciliation incident.
- **The initial tokenisation must be a cardholder-present transaction.** That
  is what establishes the stored credential the later palm-triggered charges
  depend on, and the scheme reference must be quoted on every subsequent
  merchant-initiated charge. Omitting it is a common cause of unexplained
  declines.

---

## D6. Where the threshold came from

The match threshold *is* the false-accept rate, so it was measured, not chosen.
`backend/scripts/benchmark.py` sweeps it and reports FAR/FRR, EER, and the compounding
per-transaction risk across shard sizes.

Current default `0.34` comes from 80 synthetic identities × 6 samples (1,200
genuine and 3,160 impostor pairs): FAR 0.000, FRR 0.0125, against an EER of
0.00057 at 0.390. It sits deliberately *below* the EER point, because the two
errors are not worth trading evenly — a false accept charges the wrong person,
a false reject asks someone to scan again.

**This number is valid for the prototype only.** Synthetic identities are
statistically independent; real palms share anatomical structure, so real FAR
will be worse by an unknown margin. Re-run against CASIA-Palmprint, IITD or
PolyU before this system touches money.

One finding from that work is worth carrying forward: **ROI normalisation
dominates everything else.** Anchoring ROI position and scale on the palm's
inscribed circle instead of the finger-valley span moved the EER from 13.2% to
0.06% without touching the matcher. Valley *positions* are stable; the distance
between them drifted over 10% under small rotations, rescaling the ROI and
misaligning the crease pattern. If accuracy needs to improve further, look at
capture before looking at the algorithm.

---

---

## D7. The app is the customer's, not the merchant's

**Decision:** Palmdrino is a **customer-facing** app. A person installs it,
signs up, enrols their own palm, and links **one** card. Paying happens later at
a merchant terminal, where they present their palm — the customer app is not
involved in the transaction at all.

This is a correction to what was built first. The current Android app is
terminal-shaped: "Take a payment", "Enroll a customer", "the customer types this
at the till", a merchant ID in settings. That is an operator running a till, not
a person enrolling themselves. Tracked as PD-26 and PD-27.

**One card per customer.** Already structural rather than conventional: a
profile holds a single `enc_payment_token`, so the schema cannot express a
second one. `update_payment_token()` exists to replace it, though nothing calls
it yet (PD-28). Multi-card would mean choosing *which* card a palm charges,
which needs a selection mechanism the palm alone cannot provide — so one card is
the right constraint for now, not just the simple one.

**What self-enrollment changes.** Operator-supervised enrollment has a human
confirming a real person is present. Self-enrollment on the customer's own
device removes that check, which shifts weight onto two things that were already
tracked: liveness at enrolment (PD-02) and cardholder verification when the card
is linked (PD-06). Enrolling a palm against a stolen card is the attack to keep
in view, and the card leg is where it gets stopped.

---

## D8. Three separate grants, not one shared key

**Decision:** the API recognises three distinct authorities, and none of them is
a superset of another.

| Grant | Proves | Reaches |
|---|---|---|
| **Customer** — `Authorization: Bearer <customer_id>.<secret>` | you are this customer | that customer's own account only |
| **Terminal** — `X-Api-Key` | you are a merchant terminal | `/v1/pay` |
| **Admin** — `X-Admin-Key` | you are an operator | `/v1/audit` |

`/v1/enroll` and `/v1/capture/check` are deliberately open: a customer
enrolling themselves has no credential yet, and enrollment is the call that
mints one. That makes them the most exposed endpoints in the service and the
first that need rate limiting (PD-07).

**Why this had to change.** Previously a single shared API key gated everything,
including `/v1/customers/{id}` GET and DELETE — so knowing a customer id was
enough to read their linked card and erase their profile. That held up while
the only callers were a handful of trusted terminals. Under D7 it collapses: a
customer-facing app ships its key to every install, so every user would hold
credentials that read and erase every other user.

**Customer credentials are device-bound, not passwords.** The server mints 32
random bytes at enrollment and returns them once; the device keeps them, the
server keeps only a peppered hash. No password for the customer to choose or
reuse, no SMS provider to pay for, and a database leak yields nothing
replayable. Verification is constant-time, and a mismatch answers 403 whether
or not the target id exists, so ids cannot be probed.

The cost is deliberate: **lose the device, lose access.** For a payment
credential that is the right failure mode, but it needs a recovery path before
real customers exist — PD-30.

Since the secret is server-generated and high-entropy, it is stored under a
plain keyed hash rather than a password KDF. There is no dictionary to attack,
so bcrypt-style stretching would buy nothing.

---

## D9. Fraud controls are durable and shared, or they are not controls

**Decision:** the PSD2 low-value counters and the rate limiter both live in the
database, not in process memory.

Both started in memory, and both were wrong for the same reason. A low-value
exemption counter that resets on restart, and that the next till cannot see,
lets a customer refresh their allowance by walking to another checkout. A rate
limiter with the same property forgets an attacker between deploys, and does not
exist at all behind a second server. Neither is a cache; both are controls, and
a control that can be reset by the party it constrains is decoration.

**Rate limiting uses fixed windows**, which admit up to 2x the limit across a
boundary. Accepted knowingly: the purpose is to stop enumeration and resource
exhaustion, not to meter a quota to the request, and a sliding log costs a row
per request to buy precision nothing here needs.

**What is limited, and why it matters now.** The customer-facing shift (D7) left
`/v1/enroll` and `/v1/capture/check` open to unauthenticated callers, because a
customer signing themselves up has no credential yet. `/v1/pay` takes a pay code
per attempt, and that code is one of the two SCA factors from D2 — a short
secret with unlimited attempts is enumerable. Limits are keyed per pay code
*and* per terminal, so one attacker cannot lock every customer out by grinding a
single code.

Bucket keys are hashed. Keying on the pay code in the clear would have turned
the rate-limit table into a directory of live pay codes.

---

## D10. The customer app and the terminal are separate apps

**Decision:** two Gradle product flavours, `customer` and `terminal`, with
separate source sets, application ids and launcher activities.

They are different products for different people. Flavours make the separation
structural: the customer build does not contain the payment-taking screen at
all, rather than hiding it behind a menu — verified by inspecting the built
APKs. It also lets the two be signed and distributed independently, which
matters because one goes to the public and the other to merchants.

This mirrors D8 exactly: the terminal holds a terminal grant and cannot read a
customer account; the customer holds a customer grant and cannot take payments.
The build boundary and the authorisation boundary agree.

**Credentials are stored encrypted.** Both flavours keep their secret — the
customer credential, the terminal key — in `EncryptedSharedPreferences` backed
by the Android Keystore, and backup is disabled. The previous build kept the API
key in memory only, so it vanished on restart and the terminal silently began
failing auth.

**Flow state moved to a ViewModel**, so a half-finished enrollment survives
activity recreation. Captured frames stay in memory and are never written to
disk: they are biometric data, and the device has no key management worth the
name for them.

---

## D11. Account lifecycle: pause, change card, erase

**Decision:** three distinct operations, because they are three distinct
intentions.

**Withdrawing consent suspends; it does not erase.** GDPR treats consent
withdrawal and erasure as different rights, and collapsing them would force a
customer who merely wants to stop using their palm into destroying their record.
A suspended profile stops matching immediately and keeps its data, so the
decision is reversible. Restoring requires a *fresh* grant against the current
policy version — consent that was withdrawn is spent.

How long a suspended profile may be retained before erasure becomes mandatory is
a legal question (PD-01), not a number to invent.

**Changing the card does not touch the palm.** Cards expire and get reissued
every few years; palms do not. Tying them together would mean a biometric
re-enrollment for a purely financial event. Only the payment-token field is
resealed, under the same DEK.

**Refunds are authorised against our own payment record**, not the gateway's.
The gateway knows a transaction exists but not who is entitled to reverse it, so
trusting the caller's merchant id would let any terminal refund another
merchant's takings to the cardholder. Unknown and foreign transaction ids answer
identically, so ids cannot be probed from another till.

---

## D12. A customer may enrol more than one palm

**Decision:** templates live in their own table, one row per enrolled hand.

One hand is a single point of failure for someone with a bandage, a cast, or a
dressing — situations where a person is *more* likely to be out shopping
one-handed, not less. Identification scores a candidate as the best of their
enrolled hands: presenting either should work, and taking the worst would reject
a valid customer for the crime of owning a second palm.

**Re-enrolling the same hand is refused.** Two near-identical templates on one
profile would sit inside the margin check from D1, and every payment would then
be declined as ambiguous — the customer would have quietly broken their own
account.

``engine_id`` moved onto the template rather than the profile, so an engine
upgrade can be rolled out hand by hand.

## Still open

Open work lives in [`BACKLOG.md`](BACKLOG.md), not here — one source of truth
per item. This file records what was decided; that one records what is not done.

The spec's own open questions map onto it as follows:

| Spec | Question | Ticket |
|---|---|---|
| §6.1 | GDPR Art. 9 basis, DPIA, EU AI Act classification | PD-01 |
| §6.4 | Certified presentation-attack detection | PD-02 |
| §8.3 | Which Nexi product supports biometric-triggered card-on-file charges | PD-09 |
| §8.4 | Hardware target: phone camera vs dedicated NIR terminal | PD-19 |
| §8.5 | Partner with a licensed PSP vs pursue own authorisation | PD-18 |

### The liveness gap is the one that should worry you

Under this design the palm carries the inherence factor, so defeated liveness
means unauthenticated payments. What is implemented rejects the cheap attacks —
a printout, a photo on a screen — using passive image statistics, and it is
tested against both. It is **not** certified anti-spoofing.

The secret pay code from D2 helps more than it might appear: a spoofed palm on
its own gets an attacker nothing, because they also need the code. But four
digits is low entropy and codes get shoulder-surfed at a till, so this lowers
the risk rather than removing it. Compliance and anti-spoofing are different
problems and neither mechanism substitutes for the other.

This remains the largest gap between what is here and what could take payments.
Tracked as PD-02.
