package it.palmdrino.payment.data

import android.content.Context
import com.squareup.moshi.Moshi
import com.squareup.moshi.kotlin.reflect.KotlinJsonAdapterFactory
import it.palmdrino.payment.BuildConfig
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.HttpException
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory
import java.util.UUID
import java.util.concurrent.TimeUnit

private val JPEG = "image/jpeg".toMediaType()
private val TEXT = "text/plain".toMediaType()

fun String.asPart(): RequestBody = toRequestBody(TEXT)

fun ByteArray.asImagePart(fieldName: String, fileName: String): MultipartBody.Part =
    MultipartBody.Part.createFormData(fieldName, fileName, toRequestBody(JPEG))

/**
 * Builds the API client and turns transport failures into [ApiException].
 *
 * Every call site then handles one exception type carrying a stable server
 * error code, instead of separately unpacking HTTP status codes, Retrofit
 * errors and socket timeouts.
 */
object PalmdrinoClient {

    private val moshi: Moshi = Moshi.Builder()
        .add(KotlinJsonAdapterFactory())
        .build()

    private val errorAdapter = moshi.adapter(ApiError::class.java)

    @Volatile
    private var cached: Pair<String, PalmdrinoApi>? = null

    fun api(baseUrl: String): PalmdrinoApi {
        cached?.let { (url, api) -> if (url == baseUrl) return api }
        val built = build(baseUrl)
        cached = baseUrl to built
        return built
    }

    private fun build(baseUrl: String): PalmdrinoApi {
        val logging = HttpLoggingInterceptor().apply {
            // Headers only, never bodies: request bodies here contain palm
            // images and card data, and logcat is readable by adb.
            level = if (BuildConfig.DEBUG) {
                HttpLoggingInterceptor.Level.HEADERS
            } else {
                HttpLoggingInterceptor.Level.NONE
            }
        }

        // Terminal grant only. The customer grant is passed per call as an
        // explicit Authorization header, so a credential is never attached to
        // the open endpoints (enroll, capture-check) by accident.
        val apiKeyInterceptor = Interceptor { chain ->
            val key = apiKey
            val request = if (key.isNullOrBlank()) {
                chain.request()
            } else {
                chain.request().newBuilder().header("X-Api-Key", key).build()
            }
            chain.proceed(request)
        }

        val client = OkHttpClient.Builder()
            .addInterceptor(apiKeyInterceptor)
            .addInterceptor(logging)
            .connectTimeout(10, TimeUnit.SECONDS)
            // Generous: identification unwraps and compares every candidate in
            // the shard, so a busy shard takes longer than a plain CRUD call.
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .build()

        return Retrofit.Builder()
            .baseUrl(if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/")
            .client(client)
            .addConverterFactory(MoshiConverterFactory.create(moshi))
            .build()
            .create(PalmdrinoApi::class.java)
    }

    /** Terminal API key, matching `PALMPAY_API_KEY` on the server.
     *
     * Terminal builds load this from [SecureStore] at startup; customer builds
     * never set it, because a customer grant cannot take payments (D8).
     */
    @Volatile
    var apiKey: String? = null

    /** Format a stored credential as an Authorization header value. */
    fun bearer(credential: String): String = "Bearer $credential"

    /**
     * Runs an API call, normalising every failure mode into [ApiException].
     */
    suspend fun <T> call(block: suspend () -> T): T = try {
        block()
    } catch (error: HttpException) {
        val body = error.response()?.errorBody()?.string()
        val parsed = body?.let {
            runCatching { errorAdapter.fromJson(it) }.getOrNull()
        }
        throw ApiException(
            code = parsed?.code ?: "http_${error.code()}",
            message = parsed?.message ?: error.message(),
            httpStatus = error.code(),
            detail = parsed?.detail ?: emptyMap(),
        )
    } catch (error: ApiException) {
        throw error
    } catch (error: Exception) {
        throw ApiException(
            code = "network_error",
            message = error.message ?: "Could not reach the Palmdrino service",
            httpStatus = 0,
        )
    }

    fun newIdempotencyKey(): String = "and_${UUID.randomUUID()}"
}

/** Persists the API base URL, which differs per environment. */
class ApiSettings(context: Context) {

    private val prefs = context.getSharedPreferences("palmdrino", Context.MODE_PRIVATE)

    var baseUrl: String
        get() = prefs.getString(KEY_BASE_URL, BuildConfig.DEFAULT_API_BASE_URL)
            ?: BuildConfig.DEFAULT_API_BASE_URL
        set(value) = prefs.edit().putString(KEY_BASE_URL, value.trim()).apply()

    /** Terminal builds only: which merchant this till belongs to. */
    var merchantId: String
        get() = prefs.getString(KEY_MERCHANT, "mrc_demo_terminal") ?: "mrc_demo_terminal"
        set(value) = prefs.edit().putString(KEY_MERCHANT, value.trim()).apply()

    private companion object {
        const val KEY_BASE_URL = "base_url"
        const val KEY_MERCHANT = "merchant_id"
    }
}
