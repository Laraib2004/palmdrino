"""Envelope encryption, key binding and crypto-shred."""

from __future__ import annotations

import pytest

from palmpay.crypto.envelope import (
    CustomerCipher,
    DecryptionError,
    decrypt,
    encrypt,
    field_aad,
)
from palmpay.crypto.kms import (
    DEK_BYTES,
    KeyDestroyedError,
    SoftwareKms,
    WrappedKey,
    dek_aad,
    zeroize,
)


@pytest.fixture
def kms(tmp_path):
    return SoftwareKms(tmp_path / "keystore.json")


class TestKeyManagement:
    def test_dek_is_aes256_and_unique(self, kms):
        first, second = kms.generate_dek(), kms.generate_dek()
        assert len(first) == DEK_BYTES == 32
        assert bytes(first) != bytes(second)

    def test_wrap_unwrap_round_trip(self, kms):
        dek = kms.generate_dek()
        wrapped = kms.wrap_dek(bytes(dek), dek_aad("cus_1"))
        assert bytes(kms.unwrap_dek(wrapped, dek_aad("cus_1"))) == bytes(dek)

    def test_wrapped_key_serialisation_round_trip(self, kms):
        dek = kms.generate_dek()
        wrapped = kms.wrap_dek(bytes(dek), dek_aad("cus_1"))
        restored = WrappedKey.deserialize(wrapped.serialize())
        assert bytes(kms.unwrap_dek(restored, dek_aad("cus_1"))) == bytes(dek)

    def test_wrapped_dek_cannot_be_moved_between_customers(self, kms):
        """Database write access must not let an attacker retarget key material."""
        dek = kms.generate_dek()
        wrapped = kms.wrap_dek(bytes(dek), dek_aad("victim"))
        with pytest.raises(KeyDestroyedError):
            kms.unwrap_dek(wrapped, dek_aad("attacker"))

    def test_tampered_wrapped_key_is_detected(self, kms):
        dek = kms.generate_dek()
        wrapped = kms.wrap_dek(bytes(dek), dek_aad("cus_1"))
        corrupted = WrappedKey(
            kek_id=wrapped.kek_id,
            nonce=wrapped.nonce,
            ciphertext=bytes([wrapped.ciphertext[0] ^ 0xFF]) + wrapped.ciphertext[1:],
        )
        with pytest.raises(KeyDestroyedError):
            kms.unwrap_dek(corrupted, dek_aad("cus_1"))

    def test_keystore_persists_across_instances(self, tmp_path):
        path = tmp_path / "keystore.json"
        first = SoftwareKms(path)
        dek = first.generate_dek()
        wrapped = first.wrap_dek(bytes(dek), dek_aad("cus_1"))

        second = SoftwareKms(path)
        assert bytes(second.unwrap_dek(wrapped, dek_aad("cus_1"))) == bytes(dek)

    def test_rotation_keeps_old_keys_readable(self, kms):
        dek = kms.generate_dek()
        wrapped = kms.wrap_dek(bytes(dek), dek_aad("cus_1"))
        old_kek = kms.active_kek_id

        new_kek = kms.rotate_kek()
        assert new_kek != old_kek
        assert bytes(kms.unwrap_dek(wrapped, dek_aad("cus_1"))) == bytes(dek)

    def test_cannot_destroy_active_kek(self, kms):
        with pytest.raises(ValueError, match="active KEK"):
            kms.destroy_kek(kms.active_kek_id)

    def test_destroying_kek_shreds_everything_under_it(self, kms):
        dek = kms.generate_dek()
        wrapped = kms.wrap_dek(bytes(dek), dek_aad("cus_1"))
        old_kek = kms.active_kek_id
        kms.rotate_kek()

        kms.destroy_kek(old_kek)
        with pytest.raises(KeyDestroyedError):
            kms.unwrap_dek(wrapped, dek_aad("cus_1"))

    def test_zeroize_clears_key_material(self):
        key = bytearray(b"\xAA" * 32)
        zeroize(key)
        assert bytes(key) == bytes(32)


class TestFieldEncryption:
    def test_round_trip(self):
        dek = b"k" * 32
        aad = field_aad("cus_1", "pii")
        assert decrypt(dek, encrypt(dek, b"Maria Rossi", aad), aad) == b"Maria Rossi"

    def test_ciphertext_is_not_plaintext(self):
        dek = b"k" * 32
        blob = encrypt(dek, b"Maria Rossi", field_aad("cus_1", "pii"))
        assert b"Maria Rossi" not in blob

    def test_same_plaintext_encrypts_differently_each_time(self):
        """A fresh nonce per call: equal values must not be linkable in the DB."""
        dek = b"k" * 32
        aad = field_aad("cus_1", "pii")
        assert encrypt(dek, b"same", aad) != encrypt(dek, b"same", aad)

    def test_wrong_key_fails(self):
        aad = field_aad("cus_1", "pii")
        blob = encrypt(b"k" * 32, b"secret", aad)
        with pytest.raises(DecryptionError):
            decrypt(b"x" * 32, blob, aad)

    def test_ciphertext_cannot_be_moved_between_fields(self):
        cipher = CustomerCipher("cus_1", bytearray(b"k" * 32))
        blob = cipher.seal_text("payment_token", "tok_secret")
        with pytest.raises(DecryptionError):
            cipher.open_text("pii", blob)

    def test_ciphertext_cannot_be_moved_between_customers(self):
        blob = CustomerCipher("cus_1", bytearray(b"k" * 32)).seal_text("pii", "Maria")
        with pytest.raises(DecryptionError):
            CustomerCipher("cus_2", bytearray(b"k" * 32)).open_text("pii", blob)

    def test_tampering_is_detected(self):
        cipher = CustomerCipher("cus_1", bytearray(b"k" * 32))
        blob = bytearray(cipher.seal_text("pii", "Maria Rossi"))
        blob[-1] ^= 0xFF
        with pytest.raises(DecryptionError):
            cipher.open_text("pii", bytes(blob))

    def test_context_manager_zeroizes_on_exit(self):
        cipher = CustomerCipher("cus_1", bytearray(b"k" * 32))
        with cipher:
            pass
        assert bytes(cipher.dek) == bytes(32)


class TestCryptoShredEndToEnd:
    def test_shredded_ciphertext_survives_but_is_unreadable(self, kms):
        """The GDPR erasure property: the bytes may remain, the meaning cannot.

        Models what a backup tape looks like after erasure. The ciphertext row
        is still present and byte-identical -- there is no way to reach into an
        old backup and delete it -- but every key that could open it is gone,
        so it is permanently meaningless.
        """
        dek = kms.generate_dek()
        wrapped = kms.wrap_dek(bytes(dek), dek_aad("cus_1"))

        cipher = CustomerCipher("cus_1", bytearray(dek))
        ciphertext = cipher.seal_text("biometric_template", "pretend-template-bytes")
        cipher.close()
        zeroize(dek)

        # Crypto-shred: the wrapped DEK is destroyed. Model the KMS side of
        # that by destroying the KEK that wrapped it.
        kms.rotate_kek()
        kms.destroy_kek(wrapped.kek_id)

        assert ciphertext, "ciphertext still exists, as it would in a backup"
        with pytest.raises(KeyDestroyedError):
            kms.unwrap_dek(wrapped, dek_aad("cus_1"))

        # And no other key opens it either.
        with pytest.raises(DecryptionError):
            CustomerCipher("cus_1", kms.generate_dek()).open_text(
                "biometric_template", ciphertext
            )
