package it.palmdrino.payment.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import it.palmdrino.payment.data.ApiException
import it.palmdrino.payment.data.ApiSettings
import it.palmdrino.payment.data.HealthResponse
import it.palmdrino.payment.data.PalmdrinoClient

@Composable
fun HomeScreen(
    settings: ApiSettings,
    onEnroll: () -> Unit,
    onPay: () -> Unit,
    onSettings: () -> Unit,
) {
    var health by remember { mutableStateOf<HealthResponse?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var reloadToken by remember { mutableStateOf(0) }

    LaunchedEffect(reloadToken, settings.baseUrl) {
        health = null
        error = null
        try {
            health = PalmdrinoClient.call { PalmdrinoClient.api(settings.baseUrl).health() }
        } catch (failure: ApiException) {
            error = failure.message
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text("Palmdrino", style = MaterialTheme.typography.headlineMedium)
        Text(
            "Pay with your palm. No phone, no card, no PIN.",
            style = MaterialTheme.typography.bodyMedium,
        )

        Spacer(Modifier.height(4.dp))

        when {
            error != null -> StatusCard(
                title = "Service unreachable",
                body = "$error\n\nCheck the server address in Settings and that the " +
                    "Palmdrino API is running.",
                container = MaterialTheme.colorScheme.errorContainer,
                onContainer = MaterialTheme.colorScheme.onErrorContainer,
            )

            health == null -> BusyOverlay("Connecting to the service...")

            else -> {
                val info = health!!
                StatusCard(
                    title = "Service online",
                    body = "",
                    container = MaterialTheme.colorScheme.primaryContainer,
                    onContainer = MaterialTheme.colorScheme.onPrimaryContainer,
                ) {
                    Column(Modifier.padding(top = 8.dp)) {
                        DetailRow("Engine", info.engineId)
                        DetailRow("Enrolled palms", info.enrolledProfiles.toString())
                        DetailRow("Match threshold", info.matchThreshold.toString())
                        DetailRow("Liveness", if (info.livenessRequired) "enforced" else "off")
                        DetailRow("Gateway", info.gateway)
                    }
                }
            }
        }

        Spacer(Modifier.height(4.dp))

        Button(
            onClick = onPay,
            enabled = health != null,
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Take a payment") }

        OutlinedButton(
            onClick = onEnroll,
            enabled = health != null,
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Enroll a new customer") }

        OutlinedButton(
            onClick = onSettings,
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Settings") }

        if (error != null) {
            OutlinedButton(
                onClick = { reloadToken++ },
                modifier = Modifier.fillMaxWidth(),
            ) { Text("Retry connection") }
        }

        Spacer(Modifier.height(8.dp))
        Text(
            "Prototype build. Payments are simulated against a mock acquirer and " +
                "no real money moves.",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.7f),
        )
    }
}
