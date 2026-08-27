package it.palmdrino.payment.data

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Encrypted local storage for the device credential (PD-15).
 *
 * The credential returned at enrollment is the only thing proving this device
 * belongs to this customer, and it is issued exactly once. Two consequences,
 * both handled here:
 *
 *  * It must survive restarts, or the customer silently loses access to their
 *    own account. The previous build kept the API key in memory only.
 *  * It must not sit in plain `SharedPreferences`, which is readable on a
 *    rooted device and can be swept up by device backup. Keys are held in the
 *    Android Keystore, so the ciphertext is useless off this hardware.
 *
 * Backup is disabled at the manifest level as well, so the encrypted blob is
 * never copied to another device where it could not be decrypted anyway.
 */
class SecureStore(context: Context) {

    private val prefs: SharedPreferences = try {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            "palmdrino_secure",
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    } catch (error: Exception) {
        // Keystore can be unavailable on a small number of damaged devices.
        // Failing open to plaintext would silently downgrade the protection on
        // a payment credential, so refuse instead and let the caller decide.
        Log.e("SecureStore", "encrypted storage unavailable", error)
        throw SecureStorageUnavailable(error)
    }

    var credential: String?
        get() = prefs.getString(KEY_CREDENTIAL, null)
        set(value) = prefs.edit().apply {
            if (value == null) remove(KEY_CREDENTIAL) else putString(KEY_CREDENTIAL, value)
        }.apply()

    var customerId: String?
        get() = prefs.getString(KEY_CUSTOMER_ID, null)
        set(value) = prefs.edit().apply {
            if (value == null) remove(KEY_CUSTOMER_ID) else putString(KEY_CUSTOMER_ID, value)
        }.apply()

    /** Terminal builds only: the shared terminal API key. */
    var terminalApiKey: String?
        get() = prefs.getString(KEY_TERMINAL_KEY, null)
        set(value) = prefs.edit().apply {
            if (value == null) remove(KEY_TERMINAL_KEY) else putString(KEY_TERMINAL_KEY, value)
        }.apply()

    val isEnrolled: Boolean
        get() = !credential.isNullOrBlank() && !customerId.isNullOrBlank()

    fun saveEnrollment(customerId: String, credential: String) {
        prefs.edit()
            .putString(KEY_CUSTOMER_ID, customerId)
            .putString(KEY_CREDENTIAL, credential)
            .apply()
    }

    /** Forget this device's identity, after erasure or on sign-out. */
    fun clearEnrollment() {
        prefs.edit().remove(KEY_CUSTOMER_ID).remove(KEY_CREDENTIAL).apply()
    }

    private companion object {
        const val KEY_CREDENTIAL = "credential"
        const val KEY_CUSTOMER_ID = "customer_id"
        const val KEY_TERMINAL_KEY = "terminal_api_key"
    }
}

class SecureStorageUnavailable(cause: Throwable) : Exception(
    "Encrypted storage is unavailable on this device, so a payment credential " +
        "cannot be stored safely.",
    cause,
)
