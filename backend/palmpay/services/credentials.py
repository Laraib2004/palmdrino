"""Customer authentication.

Answers a question the system previously could not: *is this caller actually
the customer they claim to be?*

Until the product became customer-facing (D7) the only callers were a handful
of trusted merchant terminals, and a shared API key was a defensible floor.
Once every customer installs the app, that model inverts: a shared key shipped
to every install would let any user read and erase any other user's profile.

The scheme
----------
At enrollment the server mints a 32-byte random secret and returns it **once**.
The app stores it on the device; the server keeps only a peppered hash, so a
database leak yields nothing replayable.

Requests then carry ``Authorization: Bearer <customer_id>.<secret>``. The
customer id identifies which hashes to check; the secret proves ownership.

Deliberate properties
---------------------
* **The secret is never stored, logged, or returned twice.** Lose it and the
  device is locked out -- which is the intended failure mode for a payment
  credential, and why a recovery path is tracked separately (PD-30).
* **Verification is constant-time**, so response timing cannot be used to
  confirm a guess.
* **Terminal credentials are a separate grant.** A terminal API key authorises
  taking payments; it can never reach a customer's account endpoints, and vice
  versa.
* **Erasure revokes credentials**, so a device still holding one for a shredded
  profile cannot authenticate against the tombstone.

Not implemented here, and tracked: rate limiting on verification attempts
(PD-07). The secret is high-entropy so guessing is not the concern -- unbounded
attempts as a resource-exhaustion vector is.
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from dataclasses import dataclass

from ..store.models import CustomerCredential, credential_hash
from ..store.repository import Repository

SECRET_BYTES = 32
_SEPARATOR = "."


class AuthenticationError(Exception):
    """The caller could not be authenticated as the customer they claimed."""


@dataclass(frozen=True)
class IssuedCredential:
    """A freshly minted credential.

    ``secret`` is the only copy that will ever exist in plaintext. It is
    returned to the client once and then unrecoverable.
    """

    credential_id: str
    customer_id: str
    secret: str

    @property
    def bearer_token(self) -> str:
        """The value the client sends in the Authorization header."""
        return f"{self.customer_id}{_SEPARATOR}{self.secret}"


def parse_bearer_token(token: str) -> tuple[str, str]:
    """Split ``<customer_id>.<secret>`` into its parts."""
    customer_id, separator, secret = token.partition(_SEPARATOR)
    if not separator or not customer_id or not secret:
        raise AuthenticationError("malformed credential")
    return customer_id, secret


@dataclass
class CredentialService:
    repository: Repository
    pepper: bytes

    def issue(self, customer_id: str, device_label: str = "") -> IssuedCredential:
        """Mint a credential for a customer's device."""
        secret = secrets.token_urlsafe(SECRET_BYTES)
        credential = CustomerCredential(
            credential_id=f"cred_{uuid.uuid4().hex[:20]}",
            customer_id=customer_id,
            token_hash=credential_hash(self.pepper, secret),
            device_label=device_label,
        )
        self.repository.create_credential(credential)
        return IssuedCredential(
            credential_id=credential.credential_id,
            customer_id=customer_id,
            secret=secret,
        )

    def verify(self, token: str) -> str:
        """Authenticate a bearer token, returning the customer id.

        Raises ``AuthenticationError`` on any failure, with the same message
        regardless of cause -- distinguishing "no such customer" from "wrong
        secret" would let an attacker enumerate who is enrolled.
        """
        customer_id, secret = parse_bearer_token(token)
        candidate = credential_hash(self.pepper, secret)

        matched = False
        for credential in self.repository.active_credentials(customer_id):
            # compare_digest, not ==, so a timing side channel cannot reveal
            # how much of a guessed secret was correct.
            if hmac.compare_digest(credential.token_hash, candidate):
                matched = True

        if not matched:
            raise AuthenticationError("invalid credential")
        return customer_id

    def revoke_all(self, customer_id: str) -> int:
        return self.repository.revoke_credentials(customer_id)
