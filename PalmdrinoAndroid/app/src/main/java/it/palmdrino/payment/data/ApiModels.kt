package it.palmdrino.payment.data

import com.squareup.moshi.Json

/**
 * Wire models mirroring `palmpay/api/schemas.py`.
 *
 * Kept as plain data classes rather than reusing the UI state types, so a
 * change to the server contract shows up here as a compile error instead of a
 * silently-null field at runtime.
 *
 * Adapters come from Moshi's reflective `KotlinJsonAdapterFactory` rather than
 * codegen, which keeps the build free of an annotation processor. These models
 * are small and parsed once per call, so reflection costs nothing that matters
 * here. Switch to `moshi-kotlin-codegen` if the payloads ever grow hot.
 */

data class HealthResponse(
    val status: String,
    @Json(name = "engine_id") val engineId: String,
    val modality: String,
    @Json(name = "enrolled_profiles") val enrolledProfiles: Int,
    @Json(name = "match_threshold") val matchThreshold: Double,
    @Json(name = "liveness_required") val livenessRequired: Boolean,
    @Json(name = "enrollment_samples") val enrollmentSamples: Int,
    val gateway: String,
)

data class Quality(
    val ok: Boolean,
    val sharpness: Double,
    val exposure: Double,
    val contrast: Double,
    val coverage: Double,
    val reasons: List<String> = emptyList(),
)

data class Liveness(
    val passed: Boolean,
    @Json(name = "high_freq_ratio") val highFreqRatio: Double,
    @Json(name = "specular_fraction") val specularFraction: Double,
    @Json(name = "moire_peak") val moirePeak: Double,
    @Json(name = "chroma_std") val chromaStd: Double,
    val reasons: List<String> = emptyList(),
)

data class CaptureCheckResponse(
    val usable: Boolean,
    @Json(name = "palm_found") val palmFound: Boolean,
    val quality: Quality? = null,
    val liveness: Liveness? = null,
    val guidance: List<String> = emptyList(),
)

data class EnrollmentResponse(
    @Json(name = "customer_id") val customerId: String,
    @Json(name = "engine_id") val engineId: String,
    @Json(name = "card_display") val cardDisplay: String,
    @Json(name = "card_scheme") val cardScheme: String,
    @Json(name = "hint_type") val hintType: String,
    @Json(name = "sample_count") val sampleCount: Int,
    @Json(name = "max_pairwise_distance") val maxPairwiseDistance: Double,
    val quality: List<Quality> = emptyList(),
)

data class Sca(
    @Json(name = "strongly_authenticated") val stronglyAuthenticated: Boolean,
    val categories: List<String>,
    val exemption: String,
    @Json(name = "may_proceed") val mayProceed: Boolean,
    val reasons: List<String>,
)

data class PaymentResponse(
    val status: String,
    @Json(name = "customer_id") val customerId: String,
    @Json(name = "transaction_id") val transactionId: String,
    @Json(name = "amount_minor") val amountMinor: Long,
    val currency: String,
    @Json(name = "card_display") val cardDisplay: String,
    val scheme: String,
    @Json(name = "authorization_code") val authorizationCode: String = "",
    @Json(name = "decline_reason") val declineReason: String? = null,
    @Json(name = "match_distance") val matchDistance: Double,
    @Json(name = "match_margin") val matchMargin: Double? = null,
    @Json(name = "candidates_considered") val candidatesConsidered: Int,
    val sca: Sca,
)

data class CustomerResponse(
    @Json(name = "customer_id") val customerId: String,
    val status: String,
    @Json(name = "engine_id") val engineId: String,
    @Json(name = "card_display") val cardDisplay: String? = null,
    @Json(name = "created_at") val createdAt: String,
    @Json(name = "consent_active") val consentActive: Boolean,
)

data class ErasureResponse(
    @Json(name = "customer_id") val customerId: String,
    val erased: Boolean,
    val method: String = "crypto_shred",
    val detail: String,
)

/** The single error shape every endpoint returns on failure. */
data class ApiError(
    val code: String,
    val message: String,
    val detail: Map<String, Any?> = emptyMap(),
)

/**
 * A failed call, carrying the server's stable error code.
 *
 * The UI branches on [code], never on the human-readable message, so wording
 * changes on the server cannot break client behaviour.
 */
class ApiException(
    val code: String,
    message: String,
    val httpStatus: Int,
    val detail: Map<String, Any?> = emptyMap(),
) : Exception(message)
