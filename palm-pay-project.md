# Palm Payment System — Project Design & Requirements

> **Status:** Draft v0.1 — project/prototype phase, with a path to production.
> **Scope:** Enrollment app (palm capture + payment method linking) → biometric matching core → payment execution via Nexi. Target market: Italy / EU.

---

## 0. One-line summary

A user enrolls **once** (scan palm + link a card via Nexi). Afterwards they pay at any terminal by scanning their palm — no phone, no card, no PIN. The palm **is** the authentication factor. Any amount is possible because the transaction is authenticated (by the palm), not because authentication is skipped.

---

## 1. Core concept & the two non-negotiable corrections

Two things were requested that need adjusting so the system actually works. Both are called out here so the rest of the doc is built on the correct version.

### 1.1 "Anyone can pay with no prior signup"
Not physically possible. A palm can only be charged if it was previously linked to a payment method. **Enrollment is mandatory** — but it happens once, and the *payment* experience afterwards is signup-free (walk up, scan, pay). This is exactly how the Chinese palm systems and Apple Pay work: registration first, frictionless use after.

### 1.2 "Each customer has a private key that encrypts all their data; delete the key = data worthless"
The *goal* is correct and is a real technique called **crypto-shredding**. But the literal design ("a private key that encrypts the data") is cryptographically wrong and must be corrected:

- **Asymmetric private keys do not encrypt bulk data.** You encrypt bulk data with a **symmetric key** (AES-256-GCM). Private/public keys are for key exchange and signatures, not for encrypting profiles or images.
- **Correct design (envelope encryption):**
  1. Each customer gets a unique **Data Encryption Key (DEK)** — a random AES-256 key.
  2. All that customer's data (template, tokens, PII) is encrypted with their DEK.
  3. The DEK is itself encrypted ("wrapped") by a **Key Encryption Key (KEK)** held in a KMS/HSM.
  4. To delete a customer: **destroy their DEK** (and its wrapped copy). Every ciphertext encrypted under it becomes permanently unrecoverable — even though the bytes may still sit in backups.
- **Result:** you get exactly the property you wanted — "remove the profile, all their data becomes worthless" — but implemented in a way that is actually secure and auditable. This is the recommended way to satisfy GDPR erasure across backups.

**So: per-customer DEK, not "per-customer private key." The rest of the doc uses DEK.**

---

## 2. System components

| Component | Purpose |
|---|---|
| Enrollment app | One-time: capture palm, quality/liveness check, create template, link card (Nexi tokenization), create encrypted profile |
| Capture SDK | Camera control, ROI extraction, image quality gating, liveness/anti-spoof |
| Biometric engine | Feature extraction → template; 1:N matching against the template index |
| Template index | Searchable store of encrypted templates for identification |
| Key management (KMS/HSM) | Holds the KEK, wraps/unwraps per-customer DEKs, performs crypto-shred |
| Payment service | Resolves matched user → charges their Nexi card-on-file token |
| Terminal app | At point of sale: capture palm → identify → authorize → charge |
| Admin/audit | Consent records, DPIA artifacts, access logs, deletion logs |

---

## 3. Data inventory & what gets encrypted

Everything customer-linked is encrypted under that customer's **DEK**.

| Data | Store raw? | Encryption | Notes |
|---|---|---|---|
| Raw palm image | **Never persist** | n/a | Held in memory only during enrollment/capture, then discarded |
| Biometric template (feature vector) | Yes (encrypted) | DEK (AES-256-GCM) | Irreversible representation; still biometric data under GDPR |
| Nexi payment token | Yes (encrypted) | DEK | Never store PAN/CVV — Nexi tokenizes |
| PII (name, email, phone) | Yes (encrypted) | DEK | Minimize what you collect |
| Consent record | Yes | Signed, retained | Must survive deletion for legal proof, so store *proof of consent*, not the biometric |
| Wrapped DEK | Yes | KEK in HSM | Deleting this = crypto-shred |
| Audit/transaction logs | Yes | Pseudonymized | Reference a user ID, not biometric data |

**Template index caveat:** for 1:N matching the engine must compare a fresh scan against stored templates. If every template is encrypted under a *different* DEK, you can't do a naive encrypted nearest-neighbor search. This is a genuine architectural tension (see §6.2) and must be designed deliberately — it's the single most important technical decision in the project.

---

## 4. Task breakdown

### Phase A — Recognition core (prototype, no real money, no real users)
- [ ] Choose modality: palm-print (RGB camera, easy start) vs palm-vein (NIR sensor, more secure)
- [ ] Capture pipeline: hand detection → ROI extraction → quality gating
- [ ] Feature extraction → template generation
- [ ] 1:1 verification, then 1:N identification with a distance threshold
- [ ] Benchmark FAR/FRR on a public palmprint dataset (no real data collection yet)
- [ ] Liveness / anti-spoof (reject photos, printouts)
- [ ] Mocked payment step (dummy balances)

### Phase B — Enrollment app
- [ ] One-time enrollment UX: palm scan + card linking
- [ ] Envelope encryption: generate DEK, encrypt profile, wrap DEK in KMS
- [ ] Consent capture + DPIA-aligned data flows
- [ ] Deletion flow = destroy DEK (crypto-shred) + delete wrapped copy

### Phase C — Nexi integration
- [ ] Confirm Nexi product supporting **card-on-file / merchant-initiated tokenized** charges
- [ ] Tokenize card at enrollment
- [ ] Charge flow: palm match → resolve token → charge via Nexi
- [ ] Handle SCA / PSD2 exemption rules for stored-credential transactions

### Phase D — Production hardening
- [ ] PCI DSS scope minimization
- [ ] Legal: GDPR Art. 9 basis, DPIA, EU AI Act classification review
- [ ] Bank of Italy / PSP authorization question resolved (partner vs licensed)
- [ ] Pen test, anti-spoof evaluation, certification
- [ ] Pilot → scale

---

## 5. Authentication & payments (the PIN question)

- The **palm scan is the Strong Customer Authentication (SCA) inherence factor.** It replaces the PIN — it does not remove authentication.
- Because the transaction *is* authenticated (by the palm), **any amount can be charged PIN-free** — this is the UX requested and it is achievable.
- What is **not** achievable: charging with genuinely *no* authentication. PSD2 only permits that for low-value transactions (~€50/tx, cumulative caps ~€150 or 5 tx) before forcing re-auth. The acquirer cannot waive SCA — it is regulatory. But since the palm authenticates, you don't need this exemption for large amounts.
- **Hard dependency:** if the palm is doing the authentication, the palm match must be strong — real liveness and a low false-match rate. A spoofable scan = unauthenticated payments = fraud + compliance failure. This is why the recognition core is the critical path.

---

## 6. Difficulties & risks (be honest about these)

### 6.1 Regulatory (highest risk, can kill the project)
- Palm data = **GDPR Art. 9 special category**. Needs explicit consent + **mandatory DPIA**.
- **PSD2 / SCA** governs the payment leg.
- **EU AI Act** — remote biometric identification may be restricted/high-risk; needs classification review.
- **Bank of Italy** authorization if you handle funds; avoid by partnering with a licensed PSP/acquirer.
- **Action:** engage an Italian fintech/privacy lawyer before production. Non-optional.

### 6.2 The encrypted-1:N-matching tension (highest *technical* risk)
- Per-customer DEK encryption is great for privacy/erasure but conflicts with fast 1:N search over encrypted templates.
- Options to evaluate: matching inside a secure enclave (TEE) that transiently decrypts; a separately-protected match index with its own key hierarchy; or requiring a lightweight identifier at pay time to convert 1:N into 1:1 (much easier, much more accurate — worth considering even if it slightly reduces "pure palm" magic).
- **This decision must be made explicitly and documented.**

### 6.3 Biometric accuracy at scale
- FAR/FRR that look fine at 100 users degrade at 1,000,000. False-match rate is the dangerous one for payments.

### 6.4 Anti-spoofing / liveness
- Photos, printouts, replicas. Vein modality is harder to spoof than print.

### 6.5 Irreversibility & breach
- A leaked template is worse than a leaked password — you can't reissue a palm. Encryption + no-raw-image storage mitigate but don't eliminate this.

### 6.6 PCI scope
- Never touch PANs; keep everything tokenized to minimize PCI DSS burden.

---

## 7. Key hierarchy (crypto-shred, corrected design)

```
HSM / KMS
 └── KEK (Key Encryption Key)  ← never leaves HSM
       └── wraps DEK_customer_A   ← unique per customer
       └── wraps DEK_customer_B
             ...
DEK_customer_X (AES-256) encrypts:
   • biometric template
   • Nexi payment token
   • PII

Delete customer X  ⇒  destroy DEK_customer_X + its wrapped copy
                   ⇒  all of X's ciphertext is permanently unrecoverable
                      (including in backups)  =  GDPR erasure satisfied
```

---

## 8. Open questions to resolve next

1. Modality: palm-print (fast prototype) or palm-vein (production-grade security)?
2. §6.2: pure 1:N, or 1:N assisted by a light identifier at pay time?
3. Nexi: which exact product supports biometric-triggered card-on-file charges? (confirm with their partnerships/dev team early)
4. Hardware target: phone camera vs dedicated NIR terminal.
5. Partner with a licensed PSP vs pursue own authorization?

---

## 9. Recommended next step

Build **Phase A** (recognition core, Python, mocked payments) to de-risk the hardest technical part before any legal/Nexi spend. The data model should already use per-customer DEK envelope encryption so Phase B/C slot in cleanly.
