package it.palmdrino.payment.ui.customer

import android.app.Application
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import it.palmdrino.payment.data.ApiException
import it.palmdrino.payment.data.ApiSettings
import it.palmdrino.payment.data.CustomerResponse
import it.palmdrino.payment.data.PalmdrinoClient
import it.palmdrino.payment.data.SecureStore
import it.palmdrino.payment.data.asImagePart
import it.palmdrino.payment.data.asPart
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.json.JSONObject

const val POLICY_VERSION = "2026-01-v1"
const val CONSENT_PURPOSES = "biometric_processing,payment_execution"
const val REQUIRED_SAMPLES = 3

const val CONSENT_TEXT =
    "I consent to Palmdrino processing an image of my palm to create a biometric " +
        "template, and to storing that template together with a token for my " +
        "payment card, so that I can pay by presenting my palm.\n\n" +
        "I understand that my palm data is special category data under GDPR " +
        "Article 9, that I can withdraw consent at any time, and that erasing my " +
        "profile permanently destroys the encryption key for my data."

/** Which step of first-time setup the customer is on. */
enum class SetupStep { CONSENT, SCAN, DETAILS, DONE }

data class SetupState(
    val step: SetupStep = SetupStep.CONSENT,
    val consentGiven: Boolean = false,
    val samples: List<ByteArray> = emptyList(),
    val payCode: String = "",
    val confirmPayCode: String = "",
    val name: String = "",
    val email: String = "",
    val cardNumber: String = "",
    val expMonth: String = "",
    val expYear: String = "",
    val cvv: String = "",
    val busy: String? = null,
    val guidance: String? = null,
    val error: String? = null,
) {
    val samplesRemaining: Int get() = (REQUIRED_SAMPLES - samples.size).coerceAtLeast(0)

    val payCodeValid: Boolean
        get() = payCode.length in 4..8 && payCode == confirmPayCode

    val detailsValid: Boolean
        get() = payCodeValid && cardNumber.length >= 12 &&
            expMonth.isNotBlank() && expYear.isNotBlank() && cvv.length >= 3
}

data class AccountState(
    val loading: Boolean = true,
    val customer: CustomerResponse? = null,
    val busy: String? = null,
    val message: String? = null,
    val error: String? = null,
)

/**
 * State for the customer app (PD-20).
 *
 * Held in a ViewModel rather than in `remember` so a half-finished setup is not
 * thrown away when the activity is recreated. Captured frames deliberately live
 * only here and are never written to disk -- they are biometric data, and this
 * device has no key management worth the name for them.
 */
class CustomerViewModel(application: Application) : AndroidViewModel(application) {

    private val settings = ApiSettings(application)
    private val store = SecureStore(application)

    private val _setup = MutableStateFlow(SetupState())
    val setup: StateFlow<SetupState> = _setup.asStateFlow()

    private val _account = MutableStateFlow(AccountState())
    val account: StateFlow<AccountState> = _account.asStateFlow()

    val isEnrolled: Boolean get() = store.isEnrolled

    private val api get() = PalmdrinoClient.api(settings.baseUrl)

    // -- setup ----------------------------------------------------------------

    fun setConsent(granted: Boolean) = _setup.update { it.copy(consentGiven = granted) }

    fun advanceToScan() = _setup.update { it.copy(step = SetupStep.SCAN, error = null) }

    fun updateSetup(transform: (SetupState) -> SetupState) = _setup.update(transform)

    fun resetSamples() = _setup.update { it.copy(samples = emptyList(), guidance = null) }

    /**
     * Check a captured frame and keep it only if the server says it is usable.
     *
     * Validating each sample as it is taken means a bad frame costs one retry
     * now, rather than failing the whole enrollment after the third capture.
     */
    fun submitSample(jpeg: ByteArray) {
        viewModelScope.launch {
            _setup.update { it.copy(busy = "Checking the scan...", guidance = null, error = null) }
            try {
                val check = PalmdrinoClient.call {
                    api.captureCheck(jpeg.asImagePart("image", "palm.jpg"))
                }
                if (check.usable) {
                    _setup.update { state ->
                        val samples = state.samples + jpeg
                        state.copy(
                            samples = samples,
                            step = if (samples.size >= REQUIRED_SAMPLES) SetupStep.DETAILS else state.step,
                        )
                    }
                } else {
                    _setup.update {
                        it.copy(
                            guidance = check.guidance.joinToString("\n")
                                .ifBlank { "That scan could not be used. Try again." }
                        )
                    }
                }
            } catch (failure: ApiException) {
                _setup.update { it.copy(error = friendly(failure)) }
            } finally {
                _setup.update { it.copy(busy = null) }
            }
        }
    }

    fun completeSetup(onDone: () -> Unit) {
        val state = _setup.value
        viewModelScope.launch {
            _setup.update { it.copy(busy = "Setting up your palm...", error = null) }
            try {
                val pii = JSONObject()
                    .put("name", state.name)
                    .put("email", state.email)
                    .toString()

                val result = PalmdrinoClient.call {
                    api.enroll(
                        frames = state.samples.mapIndexed { index, bytes ->
                            bytes.asImagePart("frames", "palm_$index.jpg")
                        },
                        hint = state.payCode.asPart(),
                        hintType = "secret".asPart(),
                        cardNumber = state.cardNumber.asPart(),
                        cardExpMonth = state.expMonth.asPart(),
                        cardExpYear = state.expYear.asPart(),
                        cardCvv = state.cvv.asPart(),
                        cardHolder = state.name.asPart(),
                        pii = pii.asPart(),
                        consentGranted = "true".asPart(),
                        consentPurposes = CONSENT_PURPOSES.asPart(),
                        consentPolicyVersion = POLICY_VERSION.asPart(),
                        consentEvidence = CONSENT_TEXT.asPart(),
                        deviceLabel = "${Build.MANUFACTURER} ${Build.MODEL}".asPart(),
                    )
                }

                // Persist before anything else can fail: the credential is
                // returned exactly once, and losing it locks this device out of
                // the account it just created.
                store.saveEnrollment(result.customerId, result.credential)

                _setup.update { SetupState(step = SetupStep.DONE) }
                onDone()
            } catch (failure: ApiException) {
                _setup.update { it.copy(error = friendly(failure)) }
            } finally {
                _setup.update { it.copy(busy = null) }
            }
        }
    }

    // -- account --------------------------------------------------------------

    fun refreshAccount() {
        val customerId = store.customerId
        val credential = store.credential
        if (customerId == null || credential == null) {
            _account.update { it.copy(loading = false, customer = null) }
            return
        }
        viewModelScope.launch {
            _account.update { it.copy(loading = true, error = null) }
            try {
                val customer = PalmdrinoClient.call {
                    api.customer(PalmdrinoClient.bearer(credential), customerId)
                }
                _account.update { it.copy(loading = false, customer = customer) }
            } catch (failure: ApiException) {
                _account.update { it.copy(loading = false, error = friendly(failure)) }
            }
        }
    }

    fun replaceCard(number: String, month: String, year: String, cvv: String, holder: String) {
        withCredential("Updating your card...") { bearer, customerId ->
            val card = PalmdrinoClient.call {
                api.replaceCard(
                    bearer, customerId,
                    number.asPart(), month.asPart(), year.asPart(), cvv.asPart(), holder.asPart(),
                )
            }
            _account.update { it.copy(message = "Card updated to ${card.cardDisplay}") }
            refreshAccount()
        }
    }

    fun withdrawConsent() {
        withCredential("Withdrawing consent...") { bearer, customerId ->
            val state = PalmdrinoClient.call { api.withdrawConsent(bearer, customerId) }
            _account.update { it.copy(message = state.detail) }
            refreshAccount()
        }
    }

    fun restoreConsent() {
        withCredential("Restoring consent...") { bearer, customerId ->
            val state = PalmdrinoClient.call {
                api.restoreConsent(
                    bearer, customerId,
                    CONSENT_PURPOSES.asPart(), POLICY_VERSION.asPart(), CONSENT_TEXT.asPart(),
                )
            }
            _account.update { it.copy(message = state.detail) }
            refreshAccount()
        }
    }

    fun eraseAccount(onErased: () -> Unit) {
        withCredential("Erasing your data...") { bearer, customerId ->
            PalmdrinoClient.call { api.eraseCustomer(bearer, customerId) }
            // The credential is revoked server-side by erasure, so keeping it
            // here would only produce confusing 401s.
            store.clearEnrollment()
            _account.update { AccountState(loading = false) }
            onErased()
        }
    }

    private fun withCredential(busy: String, block: suspend (String, String) -> Unit) {
        val customerId = store.customerId
        val credential = store.credential
        if (customerId == null || credential == null) {
            _account.update { it.copy(error = "This device is not set up.") }
            return
        }
        viewModelScope.launch {
            _account.update { it.copy(busy = busy, error = null, message = null) }
            try {
                block(PalmdrinoClient.bearer(credential), customerId)
            } catch (failure: ApiException) {
                _account.update { it.copy(error = friendly(failure)) }
            } finally {
                _account.update { it.copy(busy = null) }
            }
        }
    }

    fun dismissMessage() = _account.update { it.copy(message = null, error = null) }
}

/** Turn a stable server error code into something a customer can act on. */
fun friendly(failure: ApiException): String = when (failure.code) {
    "liveness_failed" -> "That did not look like a live hand. Show your actual palm to the camera."
    "poor_quality" -> "The scan was not clear enough. Try again in better light."
    "palm_not_found" -> "No palm was found. Hold your open hand in front of the camera."
    "inconsistent_samples" -> "Those scans did not match. Use the same hand each time."
    "insufficient_samples" -> "More scans are needed."
    "invalid_card", "card_rejected" -> "That card was not accepted. Check the details."
    "consent_required", "consent_incomplete" -> "Consent is required to continue."
    "rate_limited" -> "Too many attempts. Please wait a little and try again."
    "unauthenticated" -> "This device is no longer signed in. You may need to set up again."
    "forbidden" -> "This device cannot access that account."
    "network_error" -> "Could not reach Palmdrino. Check your connection and the server address."
    else -> failure.message ?: "Something went wrong."
}
