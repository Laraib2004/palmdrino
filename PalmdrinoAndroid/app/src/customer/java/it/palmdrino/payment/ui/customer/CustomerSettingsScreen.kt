package it.palmdrino.payment.ui.customer

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
import androidx.compose.ui.unit.dp
import it.palmdrino.payment.data.ApiSettings

/**
 * Customer-side settings.
 *
 * Only the server address: a customer has no merchant id and no terminal key,
 * because a customer grant cannot take payments (D8).
 */
@Composable
fun CustomerSettingsScreen(onDone: () -> Unit) {
    val context = LocalContext.current
    val settings = remember { ApiSettings(context) }
    var baseUrl by remember { mutableStateOf(settings.baseUrl) }

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
            label = { Text("Palmdrino server") },
            supportingText = {
                Text(
                    "Emulator: http://10.0.2.2:8000/ reaches the host machine. " +
                        "A physical device needs the host LAN address, for example " +
                        "http://192.168.1.20:8000/"
                )
            },
            modifier = Modifier.fillMaxWidth(),
        )

        Button(
            onClick = {
                settings.baseUrl = baseUrl
                onDone()
            },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Save") }

        OutlinedButton(onClick = onDone, modifier = Modifier.fillMaxWidth()) { Text("Back") }

        Text(
            "This build talks to the server over plain HTTP so it can reach a " +
                "local development server. A real release must use TLS: every " +
                "request carries biometric data or a payment instruction.",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onBackground.copy(alpha = 0.7f),
        )
    }
}
