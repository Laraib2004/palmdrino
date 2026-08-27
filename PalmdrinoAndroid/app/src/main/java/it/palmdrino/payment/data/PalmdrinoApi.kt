package it.palmdrino.payment.data

import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.Header
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part
import retrofit2.http.Path

/**
 * The Palmdrino HTTP contract.
 *
 * Palm frames are sent as multipart JPEG. They are never written to device
 * storage: each capture goes straight from the CameraX buffer into a request
 * body and is dropped once the call completes.
 *
 * Three grants exist server-side and they are not interchangeable (D8), which
 * is why the customer calls carry an explicit `Authorization` header rather
 * than relying on a client-wide interceptor: the same client instance is used
 * for open calls (enroll, capture-check) and authenticated ones, and attaching
 * a credential to the open calls would leak it further than necessary.
 */
interface PalmdrinoApi {

    @GET("v1/health")
    suspend fun health(): HealthResponse

    /**
     * Score a frame without enrolling or charging.
     *
     * Open, and safe to call repeatedly while the user frames their hand -- it
     * creates no template and writes nothing. Rate limited server-side.
     */
    @Multipart
    @POST("v1/capture/check")
    suspend fun captureCheck(
        @Part image: MultipartBody.Part,
    ): CaptureCheckResponse

    /** Self sign-up. Open, because it is the call that mints a credential. */
    @Multipart
    @POST("v1/enroll")
    suspend fun enroll(
        @Part frames: List<MultipartBody.Part>,
        @Part("hint") hint: RequestBody,
        @Part("hint_type") hintType: RequestBody,
        @Part("card_number") cardNumber: RequestBody,
        @Part("card_exp_month") cardExpMonth: RequestBody,
        @Part("card_exp_year") cardExpYear: RequestBody,
        @Part("card_cvv") cardCvv: RequestBody,
        @Part("card_holder") cardHolder: RequestBody,
        @Part("pii") pii: RequestBody,
        @Part("consent_granted") consentGranted: RequestBody,
        @Part("consent_purposes") consentPurposes: RequestBody,
        @Part("consent_policy_version") consentPolicyVersion: RequestBody,
        @Part("consent_evidence") consentEvidence: RequestBody,
        @Part("device_label") deviceLabel: RequestBody,
    ): EnrollmentResponse

    // -- customer grant -------------------------------------------------------

    @GET("v1/customers/{customerId}")
    suspend fun customer(
        @Header("Authorization") bearer: String,
        @Path("customerId") customerId: String,
    ): CustomerResponse

    @Multipart
    @POST("v1/customers/{customerId}/card")
    suspend fun replaceCard(
        @Header("Authorization") bearer: String,
        @Path("customerId") customerId: String,
        @Part("card_number") cardNumber: RequestBody,
        @Part("card_exp_month") cardExpMonth: RequestBody,
        @Part("card_exp_year") cardExpYear: RequestBody,
        @Part("card_cvv") cardCvv: RequestBody,
        @Part("card_holder") cardHolder: RequestBody,
    ): CardResponse

    @POST("v1/customers/{customerId}/consent/withdraw")
    suspend fun withdrawConsent(
        @Header("Authorization") bearer: String,
        @Path("customerId") customerId: String,
    ): ConsentStateResponse

    @Multipart
    @POST("v1/customers/{customerId}/consent/restore")
    suspend fun restoreConsent(
        @Header("Authorization") bearer: String,
        @Path("customerId") customerId: String,
        @Part("consent_purposes") consentPurposes: RequestBody,
        @Part("consent_policy_version") consentPolicyVersion: RequestBody,
        @Part("consent_evidence") consentEvidence: RequestBody,
    ): ConsentStateResponse

    @DELETE("v1/customers/{customerId}")
    suspend fun eraseCustomer(
        @Header("Authorization") bearer: String,
        @Path("customerId") customerId: String,
    ): ErasureResponse

    // -- terminal grant -------------------------------------------------------

    @Multipart
    @POST("v1/pay")
    suspend fun pay(
        @Part image: MultipartBody.Part,
        @Part("hint") hint: RequestBody,
        @Part("amount_minor") amountMinor: RequestBody,
        @Part("merchant_id") merchantId: RequestBody,
        @Part("currency") currency: RequestBody,
        @Part("idempotency_key") idempotencyKey: RequestBody,
        @Part("description") description: RequestBody,
    ): PaymentResponse

    @Multipart
    @POST("v1/payments/{transactionId}/refund")
    suspend fun refund(
        @Path("transactionId") transactionId: String,
        @Part("merchant_id") merchantId: RequestBody,
        @Part("amount_minor") amountMinor: RequestBody?,
    ): RefundResponse

    @Multipart
    @POST("v1/payments/{transactionId}/void")
    suspend fun voidPayment(
        @Path("transactionId") transactionId: String,
        @Part("merchant_id") merchantId: RequestBody,
    ): RefundResponse
}
