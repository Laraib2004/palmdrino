"""Field-level encryption under a customer DEK.

Every customer-linked value -- biometric template, payment token, PII -- is
sealed with AES-256-GCM under that customer's own DEK. GCM is authenticated
encryption, so tampering is detected rather than silently decrypted into
garbage.

Each field is additionally bound to *(customer id, field name)* via the AEAD
additional-authenticated-data. This blocks two attacks that plain encryption
does not: moving a ciphertext between customers, and moving one between fields
of the same customer (for example swapping the payment-token blob into the
email field to have it read back out in cleartext by a benign code path).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .kms import DEK_BYTES, NONCE_BYTES

_AAD_PREFIX = "palmdrino:field:v1"


class DecryptionError(Exception):
    """Ciphertext failed authentication: wrong key, wrong field, or tampering."""


def field_aad(customer_id: str, field: str) -> bytes:
    """Bind a ciphertext to one field of one customer."""
    return f"{_AAD_PREFIX}:{customer_id}:{field}".encode("utf-8")


def encrypt(dek: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Seal ``plaintext``. Output is nonce || ciphertext || tag."""
    if len(dek) != DEK_BYTES:
        raise ValueError(f"DEK must be {DEK_BYTES} bytes")
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + AESGCM(bytes(dek)).encrypt(nonce, plaintext, aad)


def decrypt(dek: bytes, blob: bytes, aad: bytes) -> bytes:
    """Open a sealed blob produced by ``encrypt``."""
    if len(dek) != DEK_BYTES:
        raise ValueError(f"DEK must be {DEK_BYTES} bytes")
    if len(blob) <= NONCE_BYTES:
        raise DecryptionError("ciphertext too short to be valid")
    nonce, body = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return AESGCM(bytes(dek)).decrypt(nonce, body, aad)
    except InvalidTag as exc:
        raise DecryptionError("ciphertext failed authentication") from exc


@dataclass
class CustomerCipher:
    """Convenience wrapper binding a plaintext DEK to one customer.

    Intended to live for the duration of a single request. Hold it no longer
    than necessary and call ``close`` when done -- the plaintext DEK is the
    most sensitive value in the process.
    """

    customer_id: str
    dek: bytearray

    def seal(self, field: str, plaintext: bytes) -> bytes:
        return encrypt(self.dek, plaintext, field_aad(self.customer_id, field))

    def open(self, field: str, blob: bytes) -> bytes:
        return decrypt(self.dek, blob, field_aad(self.customer_id, field))

    def seal_text(self, field: str, value: str) -> bytes:
        return self.seal(field, value.encode("utf-8"))

    def open_text(self, field: str, blob: bytes) -> str:
        return self.open(field, blob).decode("utf-8")

    def close(self) -> None:
        from .kms import zeroize

        zeroize(self.dek)

    def __enter__(self) -> "CustomerCipher":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
