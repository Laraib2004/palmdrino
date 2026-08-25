package it.palmdrino.payment.data

import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.http.DELETE
import retrofit2.http.GET
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
 */
interface PalmdrinoApi {

    @GET("v1/health")
    suspend fun health(): HealthResponse

    /**
     * Score a frame without enrolling or charging.
     *
     * Safe to call repeatedly while the user is framing their hand -- it
     * creates no template and writes nothing.
     */
    @Multipart
    @POST("v1/capture/check")
    suspend fun captureCheck(
        @Part image: MultipartBody.Part,
    ): CaptureCheckResponse

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
    ): EnrollmentResponse

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

    @GET("v1/customers/{customerId}")
    suspend fun customer(@Path("customerId") customerId: String): CustomerResponse

    @DELETE("v1/customers/{customerId}")
    suspend fun eraseCustomer(@Path("customerId") customerId: String): ErasureResponse
}
