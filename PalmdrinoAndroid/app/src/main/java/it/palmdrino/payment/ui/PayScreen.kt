package it.palmdrino.payment.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import it.palmdrino.payment.camera.PalmCameraController
import it.palmdrino.payment.data.ApiException
import it.palmdrino.payment.data.ApiSettings
import it.palmdrino.payment.data.PalmdrinoClient
import it.palmdrino.payment.data.PaymentResponse
import it.palmdrino.payment.data.asImagePart
import it.palmdrino.payment.data.asPart
import kotlinx.coroutines.launch

@Composable
fun PayScreen(settings: ApiSettings, onDone: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val controller = remember { PalmCameraController() }

    var amountText by remember { mutableStateOf("") }
    var hint by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf<String?>(null) }
    var error by remember { mutableStateOf<ApiException?>(null) }
    var result by remember { mutableStateOf<PaymentResponse?>(null) }

    // Generated once per attempt and reused across retries, so a dropped
    // response followed by a retry cannot charge the customer twice.
    var idempotencyKey by remember { mutableStateOf(PalmdrinoClient.newIdempotencyKey()) }

    val amountMinor = parseEuroToMinor(amountText)

    result?.let { payment ->
        PaymentResult(
            payment = payment,
            onDone = onDone,
            onAgain = {
                result = null
                amountText = ""
                hint = ""
                idempotencyKey = PalmdrinoClient.newIdempotencyKey()
            },
        )
        return
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Take a payment", style = MaterialTheme.typography.headlineMedium)

        OutlinedTextField(
            value = amountText,
            onValueChange = { amountText = it },
            label = { Text("Amount (EUR)") },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
            isError = amountText.isNotBlank() && amountMinor == null,
            modifier = Modifier.fillMaxWidth(),
        )

        OutlinedTextField(
            value = hint,
            onValueChange = { hint = it.filter(Char::isLetterOrDigit).take(8) },
            label = { Text("Customer pay code") },
            supportingText = { Text("The customer types their secret code, then scans.") },
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
            modifier = Modifier.fillMaxWidth(),
        )

        CameraPanel(controller)

        Button(
            onClick = {
                scope.launch {
                    busy = "Identifying and charging..."
                    error = null
                    try {
                        val jpeg = controller.capture(context)
                        result = PalmdrinoClient.call {
                            PalmdrinoClient.api(settings.baseUrl).pay(
                                image = jpeg.asImagePart("image", "palm.jpg"),
                                hint = hint.asPart(),
                                amountMinor = amountMinor.toString().asPart(),
                                merchantId = settings.merchantId.asPart(),
                                currency = "EUR".asPart(),
                                idempotencyKey = idempotencyKey.asPart(),
                                description = "Palmdrino terminal".asPart(),
                            )
                        }
                    } catch (failure: ApiException) {
                        error = failure
                    } catch (failure: Exception) {
                        error = ApiException(
                            code = "camera_error",
                            message = failure.message ?: "Capture failed",
                            httpStatus = 0,
                        )
                    } finally {
                        busy = null
                    }
                }
            },
            enabled = busy == null && amountMinor != null && hint.length >= 4,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text(
                if (amountMinor != null) "Scan palm and charge EUR ${formatMinor(amountMinor)}"
                else "Scan palm and charge"
            )
        }

        busy?.let { BusyOverlay(it) }

        error?.let {
            StatusCard(
                title = paymentErrorTitle(it.code),
                body = paymentErrorAdvice(it.code, it.message ?: ""),
                container = MaterialTheme.colorScheme.errorContainer,
                onContainer = MaterialTheme.colorScheme.onErrorContainer,
            )
        }

        OutlinedButton(onClick = onDone, modifier = Modifier.fillMaxWidth()) { Text("Back") }
    }
}

private fun paymentErrorTitle(code: String): String = when (code) {
    "no_match" -> "Palm not recognised"
    "ambiguous_match" -> "Could not tell two customers apart"
    "liveness_failed" -> "That did not look like a live hand"
    "poor_quality" -> "Capture quality too low"
    "palm_not_found" -> "No palm found in the frame"
    "shard_overflow" -> "Too many customers share that code"
    "gateway_error" -> "The payment service failed"
    "network_error" -> "Cannot reach the service"
    "camera_error" -> "The camera failed"
    else -> "Payment declined"
}

private fun paymentErrorAdvice(code: String, message: String): String = when (code) {
    "no_match" ->
        "$message\n\nCheck the pay code was entered correctly, or ask the customer " +
            "to pay another way."
    "ambiguous_match" ->
        "$message\n\nThe transaction was refused on purpose rather than guessing " +
            "which customer to charge. Take payment another way."
    "liveness_failed" ->
        "$message\n\nAsk the customer to present their actual hand to the camera."
    else -> message
}

@Composable
private fun PaymentResult(
    payment: PaymentResponse,
    onDone: () -> Unit,
    onAgain: () -> Unit,
) {
    val approved = payment.status == "approved"
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text(
            if (approved) "Payment approved" else "Payment declined",
            style = MaterialTheme.typography.headlineMedium,
        )

        StatusCard(
            title = "EUR ${formatMinor(payment.amountMinor)}",
            body = payment.cardDisplay,
            container = if (approved) MaterialTheme.colorScheme.primaryContainer
            else MaterialTheme.colorScheme.errorContainer,
            onContainer = if (approved) MaterialTheme.colorScheme.onPrimaryContainer
            else MaterialTheme.colorScheme.onErrorContainer,
        ) {
            Column(Modifier.padding(top = 8.dp)) {
                DetailRow("Transaction", payment.transactionId)
                if (approved) {
                    DetailRow("Auth code", payment.authorizationCode)
                } else {
                    DetailRow("Reason", payment.declineReason ?: "unknown")
                }
                DetailRow("Match distance", "%.4f".format(payment.matchDistance))
                payment.matchMargin?.let {
                    DetailRow("Margin over runner-up", "%.4f".format(it))
                }
                DetailRow("Candidates compared", payment.candidatesConsidered.toString())
            }
        }

        // Surfaced because it is the difference between a compliant flow and a
        // non-compliant one, and it is invisible otherwise.
        StatusCard(
            title = if (payment.sca.stronglyAuthenticated) {
                "Strongly authenticated"
            } else {
                "Not strong authentication"
            },
            body = if (payment.sca.stronglyAuthenticated) {
                "Factors: ${payment.sca.categories.joinToString(" + ")}. Any amount " +
                    "may be charged without a PIN."
            } else {
                "Only ${payment.sca.categories.joinToString(" + ").ifBlank { "no" }} " +
                    "factor present. Exemption: ${payment.sca.exemption}."
            },
            container = MaterialTheme.colorScheme.surfaceVariant,
            onContainer = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Button(onClick = onAgain, modifier = Modifier.weight(1f)) { Text("New payment") }
            OutlinedButton(onClick = onDone, modifier = Modifier.weight(1f)) { Text("Done") }
        }
    }
}
