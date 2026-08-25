package it.palmdrino.payment.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.text.KeyboardOptions
import it.palmdrino.payment.camera.PalmCameraController
import it.palmdrino.payment.data.ApiException
import it.palmdrino.payment.data.ApiSettings
import it.palmdrino.payment.data.EnrollmentResponse
import it.palmdrino.payment.data.PalmdrinoClient
import it.palmdrino.payment.data.asImagePart
import it.palmdrino.payment.data.asPart
import kotlinx.coroutines.launch
import org.json.JSONObject

private const val POLICY_VERSION = "2026-01-v1"
private const val CONSENT_TEXT =
    "I consent to Palmdrino processing an image of my palm to create a biometric " +
        "template, and to storing that template together with a token for my " +
        "payment card, so that I can pay by presenting my palm. I understand my " +
        "palm data is special category data under GDPR Article 9, that I can " +
        "withdraw consent at any time, and that withdrawal permanently destroys " +
        "the encryption key for my data."

private const val REQUIRED_SAMPLES = 3

@Composable
fun EnrollScreen(settings: ApiSettings, onDone: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val controller = remember { PalmCameraController() }

    var consentGiven by remember { mutableStateOf(false) }
    val samples = remember { mutableStateListOf<ByteArray>() }

    var hint by remember { mutableStateOf("") }
    var name by remember { mutableStateOf("") }
    var email by remember { mutableStateOf("") }
    var cardNumber by remember { mutableStateOf("") }
    var expMonth by remember { mutableStateOf("") }
    var expYear by remember { mutableStateOf("") }
    var cvv by remember { mutableStateOf("") }

    var busy by remember { mutableStateOf<String?>(null) }
    var guidance by remember { mutableStateOf<String?>(null) }
    var error by remember { mutableStateOf<ApiException?>(null) }
    var result by remember { mutableStateOf<EnrollmentResponse?>(null) }

    val api = PalmdrinoClient.api(settings.baseUrl)

    if (result != null) {
        EnrollmentSuccess(result!!, onDone)
        return
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Enroll a customer", style = MaterialTheme.typography.headlineMedium)
        Text(
            "One-time setup. Afterwards this customer pays by palm alone.",
            style = MaterialTheme.typography.bodyMedium,
        )

        // Step 1 -- consent. Deliberately gates everything below it: no palm is
        // captured, let alone sent for processing, until this is ticked.
        StatusCard(
            title = "1. Consent",
            body = CONSENT_TEXT,
            container = MaterialTheme.colorScheme.surfaceVariant,
            onContainer = MaterialTheme.colorScheme.onSurfaceVariant,
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.padding(top = 8.dp),
            ) {
                Checkbox(checked = consentGiven, onCheckedChange = { consentGiven = it })
                Text("The customer agrees (policy $POLICY_VERSION)")
            }
        }

        if (consentGiven) {
            Text("2. Scan the palm", style = MaterialTheme.typography.titleLarge)
            Text(
                "Capture ${REQUIRED_SAMPLES} good frames of the same hand, fingers " +
                    "spread. Each one is checked before it is accepted.",
                style = MaterialTheme.typography.bodyMedium,
            )

            CameraPanel(controller)

            LinearProgressIndicator(
                progress = { samples.size.toFloat() / REQUIRED_SAMPLES },
                modifier = Modifier.fillMaxWidth(),
            )
            Text(
                "${samples.size} of $REQUIRED_SAMPLES samples captured",
                style = MaterialTheme.typography.labelMedium,
            )

            guidance?.let {
                StatusCard(
                    title = "Try again",
                    body = it,
                    container = MaterialTheme.colorScheme.secondaryContainer,
                    onContainer = MaterialTheme.colorScheme.onSecondaryContainer,
                )
            }

            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    onClick = {
                        scope.launch {
                            busy = "Checking the capture..."
                            guidance = null
                            error = null
                            try {
                                val jpeg = controller.capture(context)
                                val check = PalmdrinoClient.call {
                                    api.captureCheck(jpeg.asImagePart("image", "palm.jpg"))
                                }
                                if (check.usable) {
                                    samples.add(jpeg)
                                } else {
                                    guidance = check.guidance.joinToString("\n")
                                        .ifBlank { "That frame could not be used. Try again." }
                                }
                            } catch (failure: ApiException) {
                                error = failure
                            } catch (failure: Exception) {
                                guidance = failure.message ?: "The camera could not capture a frame."
                            } finally {
                                busy = null
                            }
                        }
                    },
                    enabled = busy == null && samples.size < REQUIRED_SAMPLES,
                ) { Text("Capture") }

                OutlinedButton(
                    onClick = { samples.clear(); guidance = null },
                    enabled = busy == null && samples.isNotEmpty(),
                ) { Text("Reset") }
            }
        }

        if (samples.size >= REQUIRED_SAMPLES) {
            Spacer(Modifier.height(4.dp))
            Text("3. Details and card", style = MaterialTheme.typography.titleLarge)

            OutlinedTextField(
                value = hint,
                onValueChange = { hint = it.filter(Char::isLetterOrDigit).take(8) },
                label = { Text("Secret pay code (4-8 digits)") },
                supportingText = {
                    Text(
                        "The customer types this at the till before scanning. It " +
                            "narrows the search and, because it is secret, it is the " +
                            "second authentication factor PSD2 requires."
                    )
                },
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
                modifier = Modifier.fillMaxWidth(),
            )

            OutlinedTextField(
                value = name,
                onValueChange = { name = it },
                label = { Text("Name") },
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = email,
                onValueChange = { email = it },
                label = { Text("Email") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email),
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = cardNumber,
                onValueChange = { cardNumber = it.filter(Char::isDigit).take(19) },
                label = { Text("Card number (Visa or Mastercard)") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                modifier = Modifier.fillMaxWidth(),
            )
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(
                    value = expMonth,
                    onValueChange = { expMonth = it.filter(Char::isDigit).take(2) },
                    label = { Text("MM") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.weight(1f),
                )
                OutlinedTextField(
                    value = expYear,
                    onValueChange = { expYear = it.filter(Char::isDigit).take(4) },
                    label = { Text("YYYY") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.weight(1f),
                )
                OutlinedTextField(
                    value = cvv,
                    onValueChange = { cvv = it.filter(Char::isDigit).take(4) },
                    label = { Text("CVV") },
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
                    modifier = Modifier.weight(1f),
                )
            }

            Text(
                "Prototype only: a production build must collect the card in a " +
                    "gateway-hosted field so the number never reaches our servers.",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.7f),
            )

            Button(
                onClick = {
                    scope.launch {
                        busy = "Enrolling..."
                        error = null
                        try {
                            val pii = JSONObject()
                                .put("name", name)
                                .put("email", email)
                                .toString()
                            result = PalmdrinoClient.call {
                                api.enroll(
                                    frames = samples.mapIndexed { index, bytes ->
                                        bytes.asImagePart("frames", "palm_$index.jpg")
                                    },
                                    hint = hint.asPart(),
                                    hintType = "secret".asPart(),
                                    cardNumber = cardNumber.asPart(),
                                    cardExpMonth = expMonth.asPart(),
                                    cardExpYear = expYear.asPart(),
                                    cardCvv = cvv.asPart(),
                                    cardHolder = name.asPart(),
                                    pii = pii.asPart(),
                                    consentGranted = "true".asPart(),
                                    consentPurposes =
                                        "biometric_processing,payment_execution".asPart(),
                                    consentPolicyVersion = POLICY_VERSION.asPart(),
                                    consentEvidence = CONSENT_TEXT.asPart(),
                                )
                            }
                        } catch (failure: ApiException) {
                            error = failure
                        } finally {
                            busy = null
                        }
                    }
                },
                enabled = busy == null && hint.length >= 4 && cardNumber.length >= 12 &&
                    expMonth.isNotBlank() && expYear.isNotBlank() && cvv.isNotBlank(),
                modifier = Modifier.fillMaxWidth(),
            ) { Text("Complete enrollment") }
        }

        busy?.let { BusyOverlay(it) }

        error?.let {
            StatusCard(
                title = enrollmentErrorTitle(it.code),
                body = it.message ?: "",
                container = MaterialTheme.colorScheme.errorContainer,
                onContainer = MaterialTheme.colorScheme.onErrorContainer,
            )
        }

        OutlinedButton(onClick = onDone, modifier = Modifier.fillMaxWidth()) {
            Text("Cancel")
        }
    }
}

private fun enrollmentErrorTitle(code: String): String = when (code) {
    "consent_required", "consent_incomplete" -> "Consent is required"
    "liveness_failed" -> "That did not look like a live hand"
    "poor_quality" -> "Capture quality too low"
    "inconsistent_samples" -> "The samples do not match each other"
    "palm_not_found" -> "No palm found in the frame"
    "insufficient_samples" -> "More samples needed"
    "card_rejected", "invalid_card" -> "Card was rejected"
    "network_error" -> "Cannot reach the service"
    else -> "Enrollment failed"
}

@Composable
private fun EnrollmentSuccess(result: EnrollmentResponse, onDone: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text("Enrolled", style = MaterialTheme.typography.headlineMedium)
        StatusCard(
            title = result.cardDisplay,
            body = "This customer can now pay by palm.",
            container = MaterialTheme.colorScheme.primaryContainer,
            onContainer = MaterialTheme.colorScheme.onPrimaryContainer,
        ) {
            Column(Modifier.padding(top = 8.dp)) {
                DetailRow("Customer", result.customerId)
                DetailRow("Samples", result.sampleCount.toString())
                DetailRow("Sample agreement", "%.3f".format(result.maxPairwiseDistance))
                DetailRow("Engine", result.engineId)
            }
        }
        Button(onClick = onDone, modifier = Modifier.fillMaxWidth()) { Text("Done") }
    }
}
