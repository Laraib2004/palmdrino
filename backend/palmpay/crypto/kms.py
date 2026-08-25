"""Key management: the KEK, DEK wrapping, and crypto-shred.

Implements the key hierarchy from the design document:

    KEK (never leaves the KMS)
      wraps DEK_customer_A   <- unique per customer
      wraps DEK_customer_B
      ...

Destroying a customer DEK renders every ciphertext encrypted under it
permanently unrecoverable, including copies sitting in backups. That is the
GDPR erasure mechanism for this system: you cannot reach into a backup tape to
delete a row, but you can make the row meaningless.

PRODUCTION WARNING
------------------
``SoftwareKms`` keeps the KEK in a local file. That is adequate for the
prototype and for tests, and unacceptable in production: a KEK on the same disk
as the ciphertext defeats the whole hierarchy. In production this class is
replaced by an HSM- or cloud-KMS-backed implementation of ``KeyManager`` where
the KEK is non-exportable and every wrap/unwrap is an audited API call. The
interface is deliberately narrow so that swap is small.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEK_BYTES = 32  # AES-256
KEK_BYTES = 32
NONCE_BYTES = 12  # GCM standard nonce length


class KeyDestroyedError(Exception):
    """Raised when key material required to decrypt no longer exists.

    In normal operation this is not a fault: it is the expected outcome of a
    crypto-shred, and it means erasure worked.
    """


@dataclass(frozen=True)
class WrappedKey:
    """A customer DEK encrypted under a KEK.

    Carries ``kek_id`` so KEK rotation does not require re-wrapping every
    customer key at once -- old KEKs stay available for unwrapping until a
    background re-wrap completes.
    """

    kek_id: str
    nonce: bytes
    ciphertext: bytes

    def serialize(self) -> bytes:
        payload = {
            "v": 1,
            "kek_id": self.kek_id,
            "nonce": base64.b64encode(self.nonce).decode("ascii"),
            "ct": base64.b64encode(self.ciphertext).decode("ascii"),
        }
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    @classmethod
    def deserialize(cls, blob: bytes) -> "WrappedKey":
        payload = json.loads(blob.decode("utf-8"))
        if payload.get("v") != 1:
            raise ValueError("unsupported wrapped-key version")
        return cls(
            kek_id=payload["kek_id"],
            nonce=base64.b64decode(payload["nonce"]),
            ciphertext=base64.b64decode(payload["ct"]),
        )


class KeyManager(Protocol):
    """Narrow interface an HSM/cloud-KMS backend must satisfy."""

    def generate_dek(self) -> bytearray: ...

    def wrap_dek(self, dek: bytes, aad: bytes) -> WrappedKey: ...

    def unwrap_dek(self, wrapped: WrappedKey, aad: bytes) -> bytearray: ...


def dek_aad(customer_id: str) -> bytes:
    """Additional authenticated data binding a wrapped DEK to its owner.

    Without this, a wrapped DEK blob could be copied from one customer record
    to another and would still unwrap successfully -- letting an attacker with
    database write access decrypt victim A by pointing victim B key material at
    A ciphertext. With it, the unwrap simply fails.
    """
    return f"palmdrino:dek:v1:{customer_id}".encode("utf-8")


class SoftwareKms:
    """File-backed KMS stand-in. Implements ``KeyManager``.

    See the production warning in the module docstring before using this
    anywhere real.
    """

    def __init__(self, keystore_path: str | os.PathLike[str]) -> None:
        self.keystore_path = Path(keystore_path)
        self._keks: dict[str, bytes] = {}
        self._active_kek_id: str = ""
        self._load_or_initialise()

    # -- keystore persistence -------------------------------------------------

    def _load_or_initialise(self) -> None:
        if self.keystore_path.exists():
            self._load()
            return
        self.keystore_path.parent.mkdir(parents=True, exist_ok=True)
        kek_id = self._new_kek_id()
        self._keks = {kek_id: secrets.token_bytes(KEK_BYTES)}
        self._active_kek_id = kek_id
        self._save()

    def _load(self) -> None:
        payload = json.loads(self.keystore_path.read_text(encoding="utf-8"))
        self._keks = {
            kek_id: base64.b64decode(material)
            for kek_id, material in payload["keks"].items()
        }
        self._active_kek_id = payload["active"]
        if self._active_kek_id not in self._keks:
            raise ValueError("keystore active KEK is missing from the key set")

    def _save(self) -> None:
        payload = {
            "active": self._active_kek_id,
            "keks": {
                kek_id: base64.b64encode(material).decode("ascii")
                for kek_id, material in self._keks.items()
            },
        }
        tmp = self.keystore_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.keystore_path)
        # Best-effort lockdown to the owner. On Windows this is largely
        # advisory, which is one more reason this class is not for production.
        try:
            os.chmod(self.keystore_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _new_kek_id() -> str:
        return f"kek-{secrets.token_hex(6)}"

    # -- KeyManager -----------------------------------------------------------

    @property
    def active_kek_id(self) -> str:
        return self._active_kek_id

    def generate_dek(self) -> bytearray:
        """Fresh AES-256 data encryption key for exactly one customer.

        Returned as a ``bytearray`` so the caller can overwrite it after use
        (see ``zeroize``).
        """
        return bytearray(secrets.token_bytes(DEK_BYTES))

    def wrap_dek(self, dek: bytes, aad: bytes) -> WrappedKey:
        if len(dek) != DEK_BYTES:
            raise ValueError(f"DEK must be {DEK_BYTES} bytes")
        nonce = secrets.token_bytes(NONCE_BYTES)
        aesgcm = AESGCM(self._keks[self._active_kek_id])
        ciphertext = aesgcm.encrypt(nonce, bytes(dek), aad)
        return WrappedKey(kek_id=self._active_kek_id, nonce=nonce, ciphertext=ciphertext)

    def unwrap_dek(self, wrapped: WrappedKey, aad: bytes) -> bytearray:
        kek = self._keks.get(wrapped.kek_id)
        if kek is None:
            raise KeyDestroyedError(
                f"KEK {wrapped.kek_id} no longer exists; ciphertext under it is unrecoverable"
            )
        try:
            plaintext = AESGCM(kek).decrypt(wrapped.nonce, wrapped.ciphertext, aad)
        except InvalidTag as exc:
            # Either the blob was tampered with, or it belongs to a different
            # customer than the AAD claims.
            raise KeyDestroyedError("wrapped DEK failed authentication") from exc
        return bytearray(plaintext)

    # -- lifecycle ------------------------------------------------------------

    def rotate_kek(self) -> str:
        """Introduce a new active KEK, retaining old ones for unwrapping.

        New DEKs are wrapped under the new KEK immediately. Existing wrapped
        DEKs stay readable and should be re-wrapped by a background job, after
        which the retired KEK can be destroyed.
        """
        kek_id = self._new_kek_id()
        self._keks[kek_id] = secrets.token_bytes(KEK_BYTES)
        self._active_kek_id = kek_id
        self._save()
        return kek_id

    def destroy_kek(self, kek_id: str) -> None:
        """Destroy a KEK, crypto-shredding every DEK wrapped under it.

        This is the bulk erasure lever and it is irreversible. Refuses to
        destroy the active KEK, which would brick all new enrollments.
        """
        if kek_id == self._active_kek_id:
            raise ValueError("refusing to destroy the active KEK; rotate first")
        if self._keks.pop(kek_id, None) is None:
            raise KeyError(f"unknown KEK: {kek_id}")
        self._save()


def zeroize(key: bytearray) -> None:
    """Best-effort overwrite of key material in memory.

    Honest caveat: CPython gives no guarantee here. The interpreter may have
    copied these bytes during earlier operations, and those copies are beyond
    reach. Treat this as defence in depth that shortens the exposure window,
    not as a guarantee the key is gone. A production deployment keeps plaintext
    DEKs inside an HSM or enclave so they never reach interpreter memory.
    """
    for index in range(len(key)):
        key[index] = 0
