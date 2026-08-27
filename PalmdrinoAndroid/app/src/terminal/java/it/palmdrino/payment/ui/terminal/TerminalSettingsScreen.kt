package it.palmdrino.payment.ui.terminal

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
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
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import it.palmdrino.payment.data.ApiSettings
import it.palmdrino.payment.data.PalmdrinoClient
import it.palmdrino.payment.data.SecureStore

/**
 * Terminal configuration: server, merchant identity, and the terminal key.
 *
 * The key is held in encrypted storage (PD-15) and survives restarts, so staff
 * do not re-enter it every shift -- and it is never written to plain
 * preferences, where a rooted device or a backup would expose the credential
 * that authorises taking payments.
 */
@Composable
fun TerminalSettingsScreen(onDone: () -> Unit) {
    val context = LocalContext.current
    val settings = remember { ApiSettings(context) }
    val store = remember { SecureStore(context) }

    var baseUrl by remember { mutableStateOf(settings.baseUrl) }
    var merchantId by remember { mutableStateOf(settings.merchantId) }
    var apiKey by remember { mutableStateOf(store.terminalApiKey.orEmpty()) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Terminal settings", style = MaterialTheme.typography.headlineMedium)

        OutlinedTextField(
            value = baseUrl,
            onValueChange = { baseUrl = it },
            label = { Text("Palmdrino server") },
            modifier = Modifier.fillMaxWidth(),
        )

        OutlinedTextField(
            value = merchantId,
            onValueChange = { merchantId = it },
            label = { Text("Merchant ID") },
            supportingText = { Text("Refunds are only allowed for charges this merchant took.") },
            modifier = Modifier.fillMaxWidth(),
        )

        OutlinedTextField(
            value = apiKey,
            onValueChange = { apiKey = it },
            label = { Text("Terminal key") },
            supportingText = { Text("Must match PALMPAY_API_KEY on the server.") },
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth(),
        )

        Button(
            onClick = {
                settings.baseUrl = baseUrl
                settings.merchantId = merchantId
                store.terminalApiKey = apiKey.ifBlank { null }
                PalmdrinoClient.apiKey = apiKey.ifBlank { null }
                onDone()
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Save") }

        OutlinedButton(onClick = onDone, modifier = Modifier.fillMaxWidth()) { Text("Back") }

        Text(
            "Anything holding this key can charge customers. A real deployment " +
                "must give each terminal its own identity via mutual TLS or device " +
                "attestation rather than a shared key.",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.7f),
        )
    }
}
