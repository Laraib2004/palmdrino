package it.palmdrino.payment.ui

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
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import it.palmdrino.payment.data.ApiSettings
import it.palmdrino.payment.data.PalmdrinoClient

@Composable
fun SettingsScreen(settings: ApiSettings, onDone: () -> Unit) {
    var baseUrl by remember { mutableStateOf(settings.baseUrl) }
    var merchantId by remember { mutableStateOf(settings.merchantId) }
    var apiKey by remember { mutableStateOf(PalmdrinoClient.apiKey.orEmpty()) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Settings", style = MaterialTheme.typography.headlineMedium)

        OutlinedTextField(
            value = baseUrl,
            onValueChange = { baseUrl = it },
            label = { Text("API base URL") },
            supportingText = {
                Text(
                    "Emulator: http://10.0.2.2:8000/ reaches the host machine. " +
                        "A physical device needs the host's LAN address, e.g. " +
                        "http://192.168.1.20:8000/"
                )
            },
            modifier = Modifier.fillMaxWidth(),
        )

        OutlinedTextField(
            value = merchantId,
            onValueChange = { merchantId = it },
            label = { Text("Merchant ID") },
            modifier = Modifier.fillMaxWidth(),
        )

        OutlinedTextField(
            value = apiKey,
            onValueChange = { apiKey = it },
            label = { Text("API key (optional)") },
            supportingText = { Text("Must match PALMPAY_API_KEY on the server, if set.") },
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth(),
        )

        Button(
            onClick = {
                settings.baseUrl = baseUrl
                settings.merchantId = merchantId
                PalmdrinoClient.apiKey = apiKey.ifBlank { null }
                onDone()
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Save") }

        OutlinedButton(onClick = onDone, modifier = Modifier.fillMaxWidth()) { Text("Cancel") }

        Text(
            "This build talks to the service over plain HTTP so it can reach a " +
                "local development server. A real deployment must use TLS: every " +
                "request here carries biometric data or a payment instruction.",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.7f),
        )
    }
}
