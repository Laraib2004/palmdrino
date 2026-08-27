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

**16 open, 14 closed.** Every remaining ticket is blocked on something outside
this repository: a lawyer, hardware, a dataset licence, a cloud account, Nexi's
documentation, or a decision that is yours to make.

---

## P0 — blockers before real money

#### PD-01 · Legal review: GDPR Art. 9, DPIA, EU AI Act, Bank of Italy
`compliance` · **blocked (external)** · *spec §6.1*

Palm data is special-category data; a DPIA is mandatory, not advisory. Remote
biometric identification needs an EU AI Act classification review. Handling
funds directly triggers Bank of Italy authorisation, which partnering with a
licensed PSP avoids (PD-18).

Nothing in this repository is legal advice, and no amount of engineering
substitutes for this item.

**Done when:** an Italian fintech/privacy lawyer has signed off on the lawful
basis, the DPIA is written, and the AI Act classification is documented.

---

#### PD-02 · Certified presentation-attack detection
`palmprint` · **blocked (needs hardware + external lab)** · *spec §6.4*

The largest technical gap. Current liveness is passive image statistics —
high-frequency texture, specular distribution, spectral peak prominence, chroma
spread. It rejects printouts and screen replays and is tested against both, but
it is **not** certified PAD.

The secret pay code (D2) means a spoofed palm alone is not enough, which raises
the bar materially. It does not close it: 4 digits is low entropy and codes get
shoulder-surfed at a till.

**Done when:** an active challenge (hand movement with parallax verification
across frames) is implemented, capture is multi-spectral or NIR, and an
independent ISO/IEC 30107-3 evaluation has been passed.

---

#### PD-03 · Re-benchmark on real palmprints and reset the threshold
`palmprint` · **blocked (needs dataset licence)**

The current threshold of `0.34` comes from synthetic identities, which are
statistically independent while real palms share anatomical structure. Real FAR
will be worse by an unknown margin.

`backend/scripts/benchmark.py --dataset <dir>` already supports this; the
blocker is that CASIA/IITD/PolyU each require a signed application.

**Done when:** a sweep has been run against real palmprints, the operating point
is reset from that data, and `config.py` records the new provenance.

---

#### PD-04 · Replace `SoftwareKms` with an HSM or cloud KMS
`crypto` · **blocked (needs a cloud/HSM account)**

`SoftwareKms` keeps the KEK in a local file. A KEK on the same disk as the
ciphertext it protects defeats the entire key hierarchy.

The `KeyManager` protocol is narrow, and the re-wrap job (PD-14, closed) already
handles rotation — so this is a small swap once there is somewhere to swap to.

**Done when:** the KEK is non-exportable, every wrap/unwrap is an audited API
call, and `SoftwareKms` is restricted to tests.

---

#### PD-05 · TLS everywhere, plus terminal identity
`api` · **blocked (deployment)**

Every request carries biometric data or a payment instruction, and the apps
currently talk plain HTTP to reach a dev server. Beyond TLS, anything holding
the terminal key can charge customers, so terminals need their own identity.

**Done when:** TLS is enforced, `network_security_config.xml` is dropped from
release builds, and terminals authenticate by mutual TLS or device attestation.
The `X-Api-Key` check is a floor, not a solution.

---

#### PD-06 · Move card capture to a gateway-hosted field
`payments` · **blocked (needs PD-09)** · *spec §6.6*

Enrollment currently accepts the PAN directly, which pulls the whole service
into PCI DSS scope. Production must collect the card in a gateway-hosted field
so the number never reaches our servers — which requires knowing whose gateway.

**Done when:** `/v1/enroll` accepts a gateway token instead of card fields, and
`CardDetails` is confined to tests.

---

## P1 — needed for a credible pilot

#### PD-09 · Nexi integration: confirm the product, build the adapter
`payments` · **blocked (external)** · *spec §8.3*

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
`palmprint` · open *(measurable only after PD-03)*

The highest-value accuracy work available. ROI normalisation dominates
everything else here — fixing it moved EER from 13.2% to 0.06% without touching
the matcher — and convexity-defect valley detection is the weakest component in
the repo.

MediaPipe Hands is Apache 2.0 and production-grade. It drops in behind the
existing `RegionExtractor` protocol, so nothing downstream changes. Held until
PD-03 because "wins on real data" cannot be shown against synthetic palms.

**Done when:** a MediaPipe-backed `RegionExtractor` beats the current one on
real data.

---

#### PD-11 · Evaluate a learned palmprint embedding
`palmprint` · **blocked (licence check)**

Competitive code is a deterministic, auditable baseline, not the endgame. A
learned embedding should beat it.

**Check the licence first.** Academic repos are frequently research-only or
carry no licence at all, which grants no rights. For a product charging real
cards this gate comes before any accuracy measurement. Cross-dataset
generalisation is also a known weak spot in this literature, so validate against
our own capture conditions, not the paper's numbers.

**Done when:** a candidate with a commercially usable licence has been
benchmarked and either adopted behind `FeatureExtractor` or rejected with the
numbers recorded.

---

#### PD-12 · Port the store from SQLite to Postgres
`store` · open *(needs a Postgres instance to verify)*

SQLite was the prototype choice. All SQL is standard and the shard index is the
part that must carry over verbatim. Deliberately not attempted blind: a storage
port that has never run against the target database is not a port.

**Done when:** Postgres is behind a connection pool, the shard index is present,
and the test suite passes against both.

---

#### PD-18 · Decide: partner with a licensed PSP vs own authorisation
`compliance` · **your call** · *spec §8.5*

Partnering avoids Bank of Italy authorisation and shortens PD-01 considerably.
Feeds directly into PD-09.

---

#### PD-19 · Hardware target: phone camera vs dedicated NIR terminal
`palmprint` · **your call** · *spec §8.4*

Phone camera for the prototype. Revisit alongside PD-02, since NIR is what makes
vein hard to spoof and would resolve much of the liveness gap in one move. D4
describes the seam that is already in place.

---

## P2 — quality and open questions

#### PD-23 · Package naming: `palmprint` vs `recognition`
`palmprint` · **your call**

`backend/palmpay/palmprint/` contains the registry whose whole purpose is to
abstract modality — and vein is not palmprint, so a `PALM_VEIN_NIR` engine
living under `palmprint/` will read as a contradiction. `recognition` or `palm`
would survive the NIR switch. Cosmetic, one command to change.

---

#### PD-24 · Decide whether `docs/` moves under `backend/`
`docs` · **your call**

Currently at the repository root because it covers the Android apps and
compliance decisions too, not only server code.

---

#### PD-25 · Searchable template index for true 1:N
`palmprint` · open · *idea*

Today there is no index and there cannot be one: each template is encrypted
under its own per-customer DEK, so nearest-neighbour search across them is
impossible. Identification works only because the pay code narrows to a shard
small enough to decrypt candidate-by-candidate (D1).

Making palm-only lookup possible means breaking that property somewhere:

- A separate match index under its own key hierarchy — fast, but becomes a
  single high-value target, and crypto-shred no longer covers it unless the
  index row is purged too.
- Matching inside a TEE that transiently unwraps — keeps per-customer DEKs
  intact, needs hardware, does not fix the accuracy problem below.
- Biometric hashing / cancellable templates supporting search over protected
  representations — the interesting direction, still largely research.

**The accuracy objection stands regardless of the crypto.** False accepts
compound with every comparison, so true 1:N over a large enrollment is unsafe
for payments at any threshold a real biometric reaches.

**Done when:** the three options are costed against a measured FAR budget and a
decision is recorded in `DECISIONS.md` — or the idea is closed as rejected.

---

#### PD-30 · Credential recovery path
`api` · **blocked (needs PD-02)**

Device-bound credentials (D8) mean losing the phone means losing account access
— there is no password to reset and no second factor to fall back on. Today the
only route back is full re-enrollment, which also means a new palm scan.

The palm itself is the obvious recovery factor, but recovery is exactly when an
attacker would present a spoofed one, so this depends on PD-02.

**Done when:** a customer who loses their device can regain access without
re-enrolling, and the recovery path is at least as hard to attack as the
credential it replaces.

---

## Resolved

Settled items live in [`DECISIONS.md`](DECISIONS.md); listed here as an index.

| Ref | Item |
|---|---|
| D1 | Matching architecture — 1:small-N with an identifier hint *(spec §6.2, §8.2)* |
| D2 | Correction: a palm alone is not SCA; the secret pay code supplies the second factor |
| D3 | Crypto-shredding via per-customer DEK, with owner and field binding |
| D4 | Modality — palm-print now, NIR seam in place *(spec §8.1)* |
| D5 | Payments — one gateway abstraction, scheme as metadata, Nexi mocked |
| D6 | Threshold set from measurement; ROI normalisation identified as dominant |
| D7 | Product shape — customer-facing app, self-enrollment, one card per customer |
| D8 | Three separate grants; device-bound customer credentials *(closes PD-29)* |
| D9 | Fraud controls are durable and shared *(closes PD-07, PD-08)* |
| D10 | Customer and terminal are separate apps *(closes PD-15, PD-20, PD-26, PD-27)* |
| D11 | Account lifecycle — pause, change card, erase *(closes PD-16, PD-22, PD-28)* |
| D12 | More than one palm per customer *(closes PD-21)* |
| — | PD-13 — engine-mismatch skips counted, audited, reported as `reenrollment_required` |
| — | PD-14 — KEK re-wrap job, so retired KEKs can actually be destroyed |
| — | PD-17 — shards report pressure at 75% of the cap, before customers get refused |
| — | Repository restructured: backend under `backend/`, `biometrics` renamed `palmprint` |
