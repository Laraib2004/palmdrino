# PALMDRINO

Pay with your palm. Enroll once — scan your palm, link a Visa or Mastercard —
and afterwards pay at any terminal by presenting your hand. No phone, no card,
no PIN.

This repository implements **Phase A + B** of `palm-pay-project.md`: the
recognition core, envelope-encrypted enrollment with GDPR crypto-shred, PSD2
SCA policy, an HTTP API, and an Android app that drives the whole flow.

> **Prototype.** Payments run against a mocked acquirer and no real money moves.
> Read [`docs/DECISIONS.md`](docs/DECISIONS.md) before taking any number here
> as production-ready — particularly the match threshold and the liveness gap.
> Everything not yet done is tracked in [`docs/BACKLOG.md`](docs/BACKLOG.md).

---

## Quick start

```bash
cd backend

# 1. Install
py -3.13 -m pip install -e ".[dev]"

# 2. See the whole system work, no server or device needed
py -3.13 scripts/demo.py

# 3. Run the tests
py -3.13 -m pytest

# 4. Start the API for the Android app
py -3.13 -m uvicorn palmpay.api.main:app --host 0.0.0.0 --port 8000
#    interactive docs at http://localhost:8000/docs
```

All backend commands run from `backend/`.

Build the Android app:

```bash
cd PalmdrinoAndroid
./gradlew assembleDebug     # -> app/build/outputs/apk/debug/app-debug.apk
```

In the app, open **Settings** and point it at the API. The emulator reaches the
host machine at `http://10.0.2.2:8000/`; a physical device needs the host's LAN
address.

---

## How it works

```
Android app  ──JPEG over HTTP──►  FastAPI  ──►  recognition core
                                     │              capture → ROI → template
                                     │
                                     ├──►  KMS: per-customer DEK, wrapped by a KEK
                                     ├──►  SQLite: encrypted profiles, sharded index
                                     └──►  gateway: tokenised Visa/Mastercard
```

### Enrollment (once)

1. **Consent first.** No frame is analysed before an Art. 9 basis exists.
2. Three palm samples, each gated on liveness and capture quality, then
   cross-checked against each other so one bad frame cannot become someone's
   permanent identity.
3. The card is tokenised. The PAN is never stored.
4. A fresh AES-256 **DEK** is generated; template, token and PII are sealed
   under it; the DEK is wrapped by a KEK held in the KMS.

### Payment

1. The customer enters a short **secret pay code** and presents their palm.
2. The code selects a *shard*; only palms in that shard are candidates.
3. Each candidate's DEK is unwrapped individually to decrypt only that
   candidate's template. Nothing is ever stored in the clear.
4. The best match must beat both the distance threshold and a **margin** over
   the runner-up — if two enrolled palms look alike, the transaction is refused
   rather than guessed.
5. Palm (inherence) + secret code (knowledge) = two PSD2 factor categories, so
   any amount clears without a PIN.

### Erasure

Destroying the wrapped DEK renders every ciphertext under it permanently
unreadable, **including copies already written to backups** — you cannot reach
into a backup tape to delete a row, but you can make it meaningless. Proof of
consent survives, because that is the legal record that the processing which
did happen was lawful.

---

## Two corrections to the design document

Both are implemented as described, and both are explained in full in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

**1. A palm alone is not Strong Customer Authentication.** §5 concludes that
because the palm is the inherence factor, any amount can be charged PIN-free.
PSD2 requires two elements from *different* categories; a palm is one element
from one category. The fix costs nothing, because the identifier hint already
needed for 1:small-N matching doubles as a knowledge factor — *provided it is a
customer-chosen secret rather than a public identifier like a phone number*.
That distinction is enforced in code, not assumed. The four digits the customer
types are what make the flow compliant.

**2. Search narrowing is an accuracy requirement, not a UX compromise.** False
accepts compound with every comparison performed, so 1:N over a large
enrollment is unsafe for payments at any threshold a real biometric can reach.
§6.2 is resolved in favour of 1:small-N, with per-customer encryption fully
preserved.

---

## Repository layout

```
backend/              everything server-side
  palmpay/            the installable Python package
  scripts/            benchmark and demo
  tests/              113 tests
  pyproject.toml
PalmdrinoAndroid/     the Android client
docs/
  DECISIONS.md        decisions made, and why
  BACKLOG.md          open issues, tickets, planned features
```

| Path | What it is |
|---|---|
| `backend/palmpay/palmprint/` | Capture, ROI, competitive-code features, matching, liveness |
| `backend/palmpay/crypto/` | KMS, DEK wrapping, field encryption, crypto-shred |
| `backend/palmpay/store/` | Encrypted profiles, consent records, audit log, shard index |
| `backend/palmpay/payments/` | Gateway interface, mocked Nexi acquirer, PSD2 SCA policy |
| `backend/palmpay/services/` | Enrollment and payment orchestration |
| `backend/palmpay/api/` | FastAPI app consumed by the Android client |
| `backend/scripts/benchmark.py` | FAR/FRR sweep and threshold calibration |
| `backend/scripts/demo.py` | End-to-end walkthrough including the refusals |
| `PalmdrinoAndroid/` | Compose + CameraX app: enroll, pay, settings |
| `docs/DECISIONS.md` | Decisions made, and why |
| `docs/BACKLOG.md` | Open issues, tickets and planned features |

---

## Calibrating the match threshold

The threshold *is* the false-accept rate. Measure it; never guess it.

```bash
cd backend
py -3.13 scripts/benchmark.py --identities 80 --samples 6     # synthetic
py -3.13 scripts/benchmark.py --dataset path/to/palmprints    # real data
```

The synthetic mode proves the pipeline is wired up correctly. It says nothing
about real-world accuracy — synthetic identities are statistically independent
while real palms share anatomical structure. Use CASIA-Palmprint, IITD or PolyU
before setting an operating point that matters.

---

## Configuration

All settings are overridable via `PALMPAY_*` environment variables.

| Variable | Default | Notes |
|---|---|---|
| `PALMPAY_DATA_DIR` | `~/.palmdrino` | Keystore and database location |
| `PALMPAY_MATCH_THRESHOLD` | `0.34` | Measured, see above |
| `PALMPAY_MATCH_MARGIN` | `0.04` | Required gap over the runner-up |
| `PALMPAY_MAX_CANDIDATES` | `64` | Shard ceiling; exceeding it refuses the transaction |
| `PALMPAY_REQUIRE_LIVENESS` | `true` | Never disable outside a lab |
| `PALMPAY_SHARD_PEPPER` | auto-generated | **Must** come from a secret manager in production |
| `PALMPAY_API_KEY` | unset | When set, the API requires `X-Api-Key` |

---

## Before this could take real money

Ordered by how likely each is to stop the project:

1. **Legal.** GDPR Art. 9 basis, mandatory DPIA, EU AI Act classification, and
   the Bank of Italy authorisation question. Engage an Italian
   fintech/privacy lawyer. Nothing in this repository is legal advice.
2. **Certified liveness.** The current checks reject printouts and screens but
   are not certified PAD. Since the palm carries the authentication, this is the
   largest technical gap. Needs an active challenge, NIR or multi-spectral
   capture, and an ISO/IEC 30107-3 evaluation.
3. **Real-data accuracy.** Re-benchmark on real palmprints and reset the
   threshold.
4. **Key management.** Replace `SoftwareKms` with an HSM or cloud KMS. A KEK on
   the same disk as the ciphertext defeats the hierarchy.
5. **Nexi integration.** Confirm the product that supports card-on-file charges
   triggered this way, then implement the adapter behind the existing interface.
6. **Transport and terminal identity.** TLS throughout, plus mutual TLS or
   device attestation — a terminal that can call `/v1/pay` can charge customers.

---

Copyright (c) 2026 PALMDRINO

All rights reserved.

Permission is hereby granted to any person obtaining a copy of this software and
associated documentation files to view, download, and execute the software for
personal, non-commercial, and educational purposes only.

Strictly Prohibited Actions:
1. You may not rebrand, rename, or place your own logos/trademarks on this software.
2. You may not sell, rent, lease, license, or otherwise commercially exploit this
   software or any modified versions of it.
3. You may not distribute modified versions of this software as a competing commercial product.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Developed by
