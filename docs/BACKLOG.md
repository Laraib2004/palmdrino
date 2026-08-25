# Backlog

Open issues, tickets and planned features. This is the running list of *what is
not done yet*.

Its counterpart is [`DECISIONS.md`](DECISIONS.md), which records *what was
decided and why*. Anything settled moves out of here and into that file, so
there is only ever one source of truth per item.

**Maintenance rule:** this file is updated in the same turn as any decision,
change or integration — new work becomes a ticket, finished work is closed, and
anything that turned into a design decision moves to `DECISIONS.md`.

**Priorities**
- **P0** — blocks handling real money. Non-negotiable before a live pilot.
- **P1** — needed for a credible pilot.
- **P2** — quality, ergonomics, open naming/layout questions.

**Status** — `open` · `in progress` · `blocked` · `closed`

---

## P0 — blockers before real money

#### PD-01 · Legal review: GDPR Art. 9, DPIA, EU AI Act, Bank of Italy
`compliance` · open · *spec §6.1*

Palm data is special-category data; a DPIA is mandatory, not advisory. Remote
biometric identification needs an EU AI Act classification review. Handling
funds directly triggers Bank of Italy authorisation, which partnering with a
licensed PSP avoids (see PD-18).

Nothing in this repository is legal advice, and no amount of engineering
substitutes for this item.

**Done when:** an Italian fintech/privacy lawyer has signed off on the lawful
basis, the DPIA is written, and the AI Act classification is documented.

---

#### PD-02 · Certified presentation-attack detection
`palmprint` · open · *spec §6.4*

The largest technical gap. Current liveness is passive image statistics —
high-frequency texture, specular distribution, spectral peak prominence, chroma
spread. It rejects printouts and screen replays and is tested against both, but
it is **not** certified PAD.

The secret pay code (see D2) means a spoofed palm alone is not enough, which
raises the bar materially. It does not close it: 4 digits is low entropy and
codes get shoulder-surfed at a till.

**Done when:** an active challenge (hand movement with parallax verification
across frames) is implemented, capture is multi-spectral or NIR, and an
independent ISO/IEC 30107-3 evaluation has been passed.

---

#### PD-03 · Re-benchmark on real palmprints and reset the threshold
`palmprint` · open

The current threshold of `0.34` comes from synthetic identities, which are
statistically independent while real palms share anatomical structure. Real FAR
will be worse by an unknown margin.

`backend/scripts/benchmark.py --dataset <dir>` already supports this.

**Done when:** a sweep has been run against CASIA-Palmprint / IITD / PolyU, the
operating point is reset from that data, and `config.py` records the new
provenance comment.

---

#### PD-04 · Replace `SoftwareKms` with an HSM or cloud KMS
`crypto` · open

`SoftwareKms` keeps the KEK in a local file. A KEK on the same disk as the
ciphertext it protects defeats the entire key hierarchy.

The `KeyManager` protocol is deliberately narrow so this is a small swap.

**Done when:** the KEK is non-exportable, every wrap/unwrap is an audited API
call, and `SoftwareKms` is restricted to tests.

---

#### PD-05 · TLS everywhere, plus terminal identity
`api` · open

Every request carries biometric data or a payment instruction, and the app
currently talks plain HTTP to reach a dev server. Beyond TLS, a terminal that
can call `/v1/pay` can charge customers, so terminals need to prove who they
are.

**Done when:** TLS is enforced, `network_security_config.xml` is dropped from
release builds, and terminals authenticate by mutual TLS or device attestation.
The `X-Api-Key` check is a floor, not a solution.

---

#### PD-06 · Move card capture to a gateway-hosted field
`payments` · open · *spec §6.6*

The enrollment endpoint currently accepts the PAN directly, which pulls the
whole service into PCI DSS scope. Production must collect the card in a
gateway-hosted field so the number never reaches our servers.

**Done when:** `/v1/enroll` accepts a gateway token instead of card fields, and
`CardDetails` is confined to tests.

---

#### PD-07 · Rate limiting and brute-force protection
`api` · open

There is none. Two concrete exposures:

- `/v1/pay` accepts a pay code per attempt. A short secret with unlimited
  attempts is enumerable, and the code is one of the two SCA factors.
- `/v1/capture/check` runs full segmentation and FFT work per call with no
  cost to the caller — a cheap CPU-exhaustion vector.

**Done when:** per-customer and per-terminal attempt limits with lockout exist
on `/v1/pay`, `/v1/capture/check` is throttled per terminal, and lockouts are
audited.

---

#### PD-08 · Durable, shared low-value exemption counters
`payments` · open

`LowValueTracker` is in-memory and per-process. The PSD2 low-value allowance
(~€50/tx, ~€150 cumulative, 5 transactions) is a fraud control, so today a
customer resets their allowance by walking to the next till, or by any service
restart.

**Done when:** counters are persisted, shared across terminals, and updated
transactionally with the authorisation.

---

## P1 — needed for a credible pilot

#### PD-09 · Nexi integration: confirm the product, build the adapter
`payments` · blocked *(external)* · *spec §8.3*

Which Nexi product supports card-on-file charges triggered this way is a
commercial question, still unanswered. Endpoint shapes must come from Nexi's
documentation — deliberately not guessed at.

The `PaymentGateway` protocol is ready; the required contract is written up in
`backend/palmpay/payments/nexi_mock.py`.

**Done when:** a `NexiGateway` passes the same test suite as the mock against
Nexi's sandbox, including scheme-reference propagation on merchant-initiated
charges.

---

#### PD-10 · Adopt MediaPipe Hands for ROI localisation
`palmprint` · open

The highest-value accuracy work available. ROI normalisation dominates
everything else here — fixing it moved EER from 13.2% to 0.06% without touching
the matcher — and convexity-defect valley detection is the weakest component in
the repo.

MediaPipe Hands is Apache 2.0 and production-grade. It drops in behind the
existing `RegionExtractor` protocol, so nothing downstream changes.

**Done when:** a MediaPipe-backed `RegionExtractor` is benchmarked against the
current one and wins on real data.

---

#### PD-11 · Evaluate a learned palmprint embedding
`palmprint` · open

Competitive code is a deterministic, auditable baseline, not the endgame. A
learned embedding should beat it.

**Check the licence first.** Academic repos are frequently research-only or
carry no licence at all, which grants no rights. For a product charging real
cards this gate comes before any accuracy measurement. Note also that
cross-dataset generalisation is a known weak spot in this literature, so
validate against our own capture conditions, not the paper's numbers.

**Done when:** a candidate with a commercially usable licence has been
benchmarked via `benchmark.py` and either adopted behind `FeatureExtractor` or
rejected with the numbers recorded.

---

#### PD-12 · Port the store from SQLite to Postgres
`store` · open

SQLite was the prototype choice. All SQL is standard and the shard index is the
part that must carry over verbatim.

**Done when:** Postgres is behind a connection pool, the shard index is present,
and the test suite passes against both.

---

#### PD-13 · Engine migration and re-enrollment flow
`palmprint` · open

Templates carry an `engine_id` and the matcher refuses cross-engine comparison
— correct, and it prevents a silent accuracy collapse. But at pay time a
mismatched candidate is simply skipped: the customer stops being able to pay,
nothing explains why, and the skip is not even counted in the audit record.

**Done when:** engine-mismatch skips are counted and audited, affected customers
are identifiable, and there is a re-enrollment prompt. Needed before PD-10 or
PD-11 ships, since both change the engine.

---

#### PD-14 · KEK rotation: background re-wrap job
`crypto` · open

`rotate_kek()` introduces a new active KEK and retains the old ones for
unwrapping, but nothing re-wraps existing DEKs. Retired KEKs therefore
accumulate forever and can never be destroyed, which is what rotation is for.

**Done when:** a job re-wraps DEKs under the active KEK and retired KEKs can be
safely destroyed once it completes.

---

#### PD-15 · Android: persist the API key
`android` · open

`PalmdrinoClient.apiKey` is an in-memory field. `ApiSettings` persists the base
URL and merchant ID but not the key, so it is lost on every app restart and the
terminal silently starts failing auth.

Store it in `EncryptedSharedPreferences`, not plain prefs.

**Done when:** the key survives restart and is encrypted at rest.

---

#### PD-16 · Refund and void endpoints
`api` · open

The gateway implements `capture`, `refund` and `void`, but the API exposes none
of them. A terminal that can charge but cannot refund is not usable in a real
shop.

**Done when:** refund and void are exposed, audited, and access-controlled to
the originating merchant.

---

#### PD-17 · Shard overflow strategy
`store` · open

Exceeding `max_candidates` currently refuses the transaction — deliberate, since
a hint that stopped narrowing is an accuracy regression rather than something to
paper over. But refusing the customer is not a long-term answer.

**Done when:** there is a defined response: longer codes, a secondary
discriminator, or operational alerting before shards approach the cap.

---

#### PD-18 · Decide: partner with a licensed PSP vs own authorisation
`compliance` · open · *spec §8.5*

Partnering avoids Bank of Italy authorisation and shortens PD-01 considerably.
Commercial decision, feeds directly into PD-09.

---

#### PD-19 · Hardware target: phone camera vs dedicated NIR terminal
`palmprint` · open · *spec §8.4*

Phone camera for the prototype. Revisit alongside PD-02, since NIR is what makes
vein hard to spoof and would resolve much of the liveness gap in one move. See
D4 for the seam that is already in place.

---

## P2 — quality and open questions

#### PD-20 · Android: hoist screen state out of composables
`android` · open

Enrollment and payment state lives in `remember`, so it does not survive process
death. The app is locked to portrait so rotation is not a factor, but a
half-finished enrollment being silently lost is still poor.

**Done when:** flow state is in a `ViewModel` or `rememberSaveable`.

---

#### PD-21 · Second-hand enrollment
`palmprint` · open · *feature*

A customer with one enrolled palm cannot pay with a bandaged or injured hand.
Enrolling both palms under one customer is a small change to the profile model
and a real robustness win.

**Done when:** a profile can hold multiple templates and identification tries
each.

---

#### PD-22 · Consent withdrawal as a distinct operation
`compliance` · open

Withdrawing consent and erasing data are currently the same call. They are
different rights and a customer may want the first without the second.

---

#### PD-23 · Package naming: `palmprint` vs `recognition`
`palmprint` · open

`backend/palmpay/palmprint/` contains the registry whose whole purpose is to
abstract modality — and vein is not palmprint, so a `PALM_VEIN_NIR` engine
living under `palmprint/` will read as a contradiction. `recognition` or `palm`
would survive the NIR switch. Cosmetic, one command to change.

---

#### PD-24 · Decide whether `docs/` moves under `backend/`
`docs` · open

Currently at the repository root because it covers Android and compliance
decisions too, not only server code.

---

#### PD-25 · Searchable template index for true 1:N
`palmprint` · open · *idea*

Today there is no index and there cannot be one: each template is encrypted
under its own per-customer DEK, so nearest-neighbour search across them is
impossible. Identification works only because the pay code narrows to a shard
that is small enough to decrypt candidate-by-candidate (D1).

Making palm-only lookup possible means breaking that property somewhere. Options
worth costing:

- A separate match index under its own key hierarchy — fast, but becomes a
  single high-value target, and crypto-shred no longer covers it unless the
  index row is purged too.
- Matching inside a TEE that transiently unwraps — keeps per-customer DEKs
  intact, needs hardware, and does not fix the accuracy problem below.
- Biometric hashing / cancellable templates supporting search over protected
  representations — the interesting direction, still largely research.

**The accuracy objection stands regardless of the crypto.** False accepts
compound with every comparison, so true 1:N over a large enrollment is unsafe
for payments at any threshold a real biometric reaches. Any index work has to
clear that bar first, not just the encryption one.

**Done when:** the three options are costed against a measured FAR budget and a
decision is recorded in `DECISIONS.md` — or the idea is closed as rejected.

---

#### PD-26 · Reframe the Android app as the customer app
`android` · open

The app is built as a merchant terminal but the product is customer-facing
(D7). Needs: self sign-up, own-palm enrolment, linking one card, and account
management — view the linked card, replace it, withdraw consent, erase.

Remove the operator framing throughout: "Enroll a customer", "Take a payment",
"the customer types this at the till", and the merchant ID setting.

**Done when:** a person can install the app and enrol themselves end to end
without any operator, and nothing in the UI addresses a cashier.

---

#### PD-27 · Separate merchant terminal surface
`android` · open

The pay flow currently lives in the same app as enrolment. Once PD-26 lands it
has nowhere to go. Decide whether the terminal is a second app, a build flavour,
or a role unlocked by terminal credentials.

Feeds PD-05: whatever the terminal is, it needs an identity, because anything
that can call `/v1/pay` can charge customers.

**Done when:** terminal and customer surfaces are separated and the terminal
authenticates as itself.

---

#### PD-28 · Card replacement flow
`api` · open

`Repository.update_payment_token()` exists but has no caller — no service method
and no endpoint. A customer whose card expires or is reissued currently has no
way to update it short of full re-enrolment, which would mean re-scanning their
palm for a card change.

**Done when:** an authenticated customer can replace their one card without
re-enrolling their palm, and the change is audited.

---

## Resolved

Settled items live in [`DECISIONS.md`](DECISIONS.md); listed here only as an
index.

| Ref | Item |
|---|---|
| D1 | Matching architecture — 1:small-N with an identifier hint *(resolves spec §6.2, §8.2)* |
| D2 | Correction: a palm alone is not SCA; the secret pay code supplies the second factor |
| D3 | Crypto-shredding via per-customer DEK, with owner and field binding |
| D4 | Modality — palm-print now, NIR seam in place *(resolves spec §8.1)* |
| D5 | Payments — one gateway abstraction, scheme as metadata, Nexi mocked |
| D6 | Threshold set from measurement; ROI normalisation identified as dominant |
| D7 | Product shape — customer-facing app, self-enrollment, one card per customer |
| — | Repository restructured: backend code under `backend/`, `biometrics` renamed `palmprint` |
