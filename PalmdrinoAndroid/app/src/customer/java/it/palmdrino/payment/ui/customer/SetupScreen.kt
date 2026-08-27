package it.palmdrino.payment.ui.customer

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
import androidx.compose.material3.Checkbox
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import it.palmdrino.payment.camera.PalmCameraController
import it.palmdrino.payment.ui.BusyOverlay
import it.palmdrino.payment.ui.CameraPanel
import it.palmdrino.payment.ui.StatusCard
import kotlinx.coroutines.launch

/**
 * First-time setup, written for the person whose palm it is.
 *
 * Three steps in a fixed order, and the order is the point: consent is given
 * before any frame is captured, because processing biometric data without a
 * lawful basis is not something to fix up afterwards.
 */
@Composable
fun SetupScreen(
    viewModel: CustomerViewModel,
    onDone: () -> Unit,
    onSettings: () -> Unit,
) {
    val state by viewModel.setup.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val controller = remember { PalmCameraController() }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Set up Palmdrino", style = MaterialTheme.typography.headlineMedium)
        Text(
            "Scan your palm once and link a card. After that you pay in shops by " +
                "holding out your hand -- no phone, no card, no PIN.",
            style = MaterialTheme.typography.bodyMedium,
        )

        when (state.step) {
            SetupStep.CONSENT -> ConsentStep(state, viewModel)
            SetupStep.SCAN -> ScanStep(state, viewModel, controller, context, scope)
            SetupStep.DETAILS, SetupStep.DONE -> DetailsStep(state, viewModel, onDone)
        }

        state.busy?.let { BusyOverlay(it) }

        state.error?.let {
            StatusCard(
                title = "That did not work",
                body = it,
                container = MaterialTheme.colorScheme.errorContainer,
                onContainer = MaterialTheme.colorScheme.onErrorContainer,
            )
        }

        OutlinedButton(onClick = onSettings, modifier = Modifier.fillMaxWidth()) {
            Text("Server settings")
        }
    }
}

@Composable
private fun ConsentStep(state: SetupState, viewModel: CustomerViewModel) {
    StatusCard(
        title = "Your consent",
        body = CONSENT_TEXT,
        container = MaterialTheme.colorScheme.surfaceVariant,
        onContainer = MaterialTheme.colorScheme.onSurfaceVariant,
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.padding(top = 10.dp),
        ) {
            Checkbox(
                checked = state.consentGiven,
                onCheckedChange = { viewModel.setConsent(it) },
            )
            Text("I agree (policy $POLICY_VERSION)")
        }
    }

    Button(
        onClick = { viewModel.advanceToScan() },
        enabled = state.consentGiven,
        modifier = Modifier.fillMaxWidth(),
    ) { Text("Continue") }
}

@Composable
private fun ScanStep(
    state: SetupState,
    viewModel: CustomerViewModel,
    controller: PalmCameraController,
    context: android.content.Context,
    scope: kotlinx.coroutines.CoroutineScope,
) {
    Text("Scan your palm", style = MaterialTheme.typography.titleLarge)
    Text(
        "Hold your open hand in front of the camera with your fingers spread. " +
            "We need $REQUIRED_SAMPLES good scans of the same hand.",
        style = MaterialTheme.typography.bodyMedium,
    )

    CameraPanel(controller)

    LinearProgressIndicator(
        progress = { state.samples.size.toFloat() / REQUIRED_SAMPLES },
        modifier = Modifier.fillMaxWidth(),
    )
    Text(
        if (state.samplesRemaining > 0) {
            "${state.samplesRemaining} more to go"
        } else {
            "All scans captured"
        },
        style = MaterialTheme.typography.labelMedium,
    )

    state.guidance?.let {
        StatusCard(
            title = "Try that again",
            body = it,
            container = MaterialTheme.colorScheme.secondaryContainer,
            onContainer = MaterialTheme.colorScheme.onSecondaryContainer,
        )
    }

    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        Button(
            onClick = {
                scope.launch {
                    runCatching { controller.capture(context) }
                        .onSuccess { viewModel.submitSample(it) }
                        .onFailure {
                            viewModel.updateSetup { s ->
                                s.copy(guidance = "The camera could not take a picture.")
                            }
                        }
                }
            },
            enabled = state.busy == null && state.samplesRemaining > 0,
        ) { Text("Scan") }

        OutlinedButton(
            onClick = { viewModel.resetSamples() },
            enabled = state.busy == null && state.samples.isNotEmpty(),
        ) { Text("Start over") }
    }
}

@Composable
private fun DetailsStep(
    state: SetupState,
    viewModel: CustomerViewModel,
    onDone: () -> Unit,
) {
    Text("Your details", style = MaterialTheme.typography.titleLarge)

    OutlinedTextField(
        value = state.payCode,
        onValueChange = { value ->
            viewModel.updateSetup { it.copy(payCode = value.filter(Char::isDigit).take(8)) }
        },
        label = { Text("Choose a pay code (4-8 digits)") },
        supportingText = {
            Text(
                "You type this at the till before scanning your palm. Keep it " +
                    "secret -- together with your palm it is what authorises the " +
                    "payment, which is why no PIN or card is needed."
            )
        },
        visualTransformation = PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
        modifier = Modifier.fillMaxWidth(),
    )

    OutlinedTextField(
        value = state.confirmPayCode,
        onValueChange = { value ->
            viewModel.updateSetup { it.copy(confirmPayCode = value.filter(Char::isDigit).take(8)) }
        },
        label = { Text("Confirm pay code") },
        isError = state.confirmPayCode.isNotEmpty() && state.confirmPayCode != state.payCode,
        supportingText = {
            if (state.confirmPayCode.isNotEmpty() && state.confirmPayCode != state.payCode) {
                Text("The codes do not match")
            }
        },
        visualTransformation = PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
        modifier = Modifier.fillMaxWidth(),
    )

    OutlinedTextField(
        value = state.name,
        onValueChange = { value -> viewModel.updateSetup { it.copy(name = value) } },
        label = { Text("Your name") },
        modifier = Modifier.fillMaxWidth(),
    )
    OutlinedTextField(
        value = state.email,
        onValueChange = { value -> viewModel.updateSetup { it.copy(email = value) } },
        label = { Text("Email") },
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email),
        modifier = Modifier.fillMaxWidth(),
    )

    Text("Your card", style = MaterialTheme.typography.titleLarge)
    Text(
        "One Visa or Mastercard. This is the card your palm will charge.",
        style = MaterialTheme.typography.bodyMedium,
    )

    OutlinedTextField(
        value = state.cardNumber,
        onValueChange = { value ->
            viewModel.updateSetup { it.copy(cardNumber = value.filter(Char::isDigit).take(19)) }
        },
        label = { Text("Card number") },
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        modifier = Modifier.fillMaxWidth(),
    )
    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        OutlinedTextField(
            value = state.expMonth,
            onValueChange = { value ->
                viewModel.updateSetup { it.copy(expMonth = value.filter(Char::isDigit).take(2)) }
            },
            label = { Text("MM") },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
            modifier = Modifier.weight(1f),
        )
        OutlinedTextField(
            value = state.expYear,
            onValueChange = { value ->
                viewModel.updateSetup { it.copy(expYear = value.filter(Char::isDigit).take(4)) }
            },
            label = { Text("YYYY") },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
            modifier = Modifier.weight(1f),
        )
        OutlinedTextField(
            value = state.cvv,
            onValueChange = { value ->
                viewModel.updateSetup { it.copy(cvv = value.filter(Char::isDigit).take(4)) }
            },
            label = { Text("CVV") },
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
            modifier = Modifier.weight(1f),
        )
    }

    Text(
        "Prototype build: a real release would collect your card in a field " +
            "hosted by the payment provider, so the number never reaches our " +
            "servers at all.",
        style = MaterialTheme.typography.labelMedium,
        color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.7f),
    )

    Button(
        onClick = { viewModel.completeSetup(onDone) },
        enabled = state.busy == null && state.detailsValid,
        modifier = Modifier.fillMaxWidth(),
    ) { Text("Finish setup") }
}
