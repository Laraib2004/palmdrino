"""Maintenance jobs (PD-14).

Operational work that runs on a schedule rather than in a request.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..crypto.kms import KeyDestroyedError, SoftwareKms, WrappedKey, dek_aad, zeroize
from ..store.models import ProfileStatus
from ..store.repository import Repository


@dataclass
class RewrapReport:
    examined: int
    rewrapped: int
    already_current: int
    unreadable: int
    retired_keks: list[str]


@dataclass
class KeyMaintenance:
    """Re-wraps customer DEKs under the active KEK.

    Without this, ``rotate_kek()`` only ever adds keys. Old KEKs stay live
    forever because DEKs still reference them, and a KEK that can never be
    destroyed defeats the point of rotating: rotation exists so that a
    suspected-compromised key can be taken out of service.

    Each DEK is unwrapped and re-wrapped individually. The plaintext DEK exists
    only inside one loop iteration and is zeroized immediately, so a rotation
    pass does not widen the window in which key material is in memory.

    Shredded profiles are skipped by design -- their wrapped DEK is already
    gone, and re-creating key material for an erased customer would undo the
    erasure.
    """

    repository: Repository
    kms: SoftwareKms

    def rewrap_all(self) -> RewrapReport:
        active = self.kms.active_kek_id
        examined = rewrapped = already_current = unreadable = 0

        for profile in self.repository.iter_profiles():
            if profile.status is ProfileStatus.SHREDDED or not profile.wrapped_dek:
                continue
            examined += 1

            try:
                wrapped = WrappedKey.deserialize(profile.wrapped_dek)
            except ValueError:
                unreadable += 1
                continue

            if wrapped.kek_id == active:
                already_current += 1
                continue

            aad = dek_aad(profile.customer_id)
            try:
                dek = self.kms.unwrap_dek(wrapped, aad)
            except KeyDestroyedError:
                # The KEK is already gone: this ciphertext is unrecoverable and
                # nothing here can bring it back.
                unreadable += 1
                continue

            try:
                fresh = self.kms.wrap_dek(bytes(dek), aad)
            finally:
                zeroize(dek)

            self.repository.update_wrapped_dek(profile.customer_id, fresh.serialize())
            rewrapped += 1

        retired = self.retired_keks()
        self.repository.append_audit(
            event_type="kek_rewrap",
            outcome="success",
            detail={
                "active_kek": active,
                "examined": examined,
                "rewrapped": rewrapped,
                "already_current": already_current,
                "unreadable": unreadable,
                "retired_keks_now_destroyable": retired,
            },
        )
        return RewrapReport(examined, rewrapped, already_current, unreadable, retired)

    def retired_keks(self) -> list[str]:
        """KEKs no live profile references, and which can now be destroyed."""
        active = self.kms.active_kek_id
        in_use: set[str] = set()
        for profile in self.repository.iter_profiles():
            if profile.status is ProfileStatus.SHREDDED or not profile.wrapped_dek:
                continue
            try:
                in_use.add(WrappedKey.deserialize(profile.wrapped_dek).kek_id)
            except ValueError:
                continue
        return sorted(k for k in self.kms.known_kek_ids() if k != active and k not in in_use)
