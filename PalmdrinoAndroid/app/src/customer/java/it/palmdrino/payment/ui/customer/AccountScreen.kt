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
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import it.palmdrino.payment.ui.BusyOverlay
import it.palmdrino.payment.ui.DetailRow
import it.palmdrino.payment.ui.StatusCard

/**
 * The customer's own account: what is linked, and how to change or end it.
 *
 * Consent withdrawal and erasure are offered as separate actions on purpose --
 * they are different rights, and collapsing them would force a customer who
 * merely wants to stop using their palm into destroying their record.
 */
@Composable
fun AccountScreen(
    viewModel: CustomerViewModel,
    onErased: () -> Unit,
    onSettings: () -> Unit,
) {
    val state by viewModel.account.collectAsStateWithLifecycle()
    var showCardForm by remember { mutableStateOf(false) }
    var confirmErase by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) { viewModel.refreshAccount() }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Your Palmdrino", style = MaterialTheme.typography.headlineMedium)

        when {
            state.loading -> BusyOverlay("Loading your account...")

            state.customer == null -> StatusCard(
                title = "Could not load your account",
                body = state.error ?: "Check the server address in settings.",
                container = MaterialTheme.colorScheme.errorContainer,
                onContainer = MaterialTheme.colorScheme.onErrorContainer,
            )

            else -> {
                val customer = state.customer!!
                val suspended = customer.status == "suspended"

                StatusCard(
                    title = if (suspended) "Paused" else "Ready to pay",
                    body = if (suspended) {
                        "You have withdrawn consent, so your palm cannot be used to " +
                            "pay. Your data is still here and you can restore consent " +
                            "at any time."
                    } else {
                        "Hold out your palm at any Palmdrino till and enter your pay code."
                    },
                    container = if (suspended) MaterialTheme.colorScheme.secondaryContainer
                    else MaterialTheme.colorScheme.primaryContainer,
                    onContainer = if (suspended) MaterialTheme.colorScheme.onSecondaryContainer
                    else MaterialTheme.colorScheme.onPrimaryContainer,
                ) {
                    Column(Modifier.padding(top = 8.dp)) {
                        DetailRow("Card", customer.cardDisplay ?: "none")
                        DetailRow("Status", customer.status)
                        DetailRow("Consent", if (customer.consentActive) "given" else "withdrawn")
                    }
                }

                if (suspended) {
                    Button(
                        onClick = { viewModel.restoreConsent() },
                        enabled = state.busy == null,
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text("Restore consent and start paying again") }
                } else {
                    OutlinedButton(
                        onClick = { viewModel.withdrawConsent() },
                        enabled = state.busy == null,
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text("Pause -- withdraw consent, keep my data") }
                }

                OutlinedButton(
                    onClick = { showCardForm = !showCardForm },
                    modifier = Modifier.fillMaxWidth(),
                ) { Text(if (showCardForm) "Cancel card change" else "Change my card") }

                if (showCardForm) {
                    CardForm(
                        busy = state.busy != null,
                        onSubmit = { number, month, year, cvv ->
                            viewModel.replaceCard(number, month, year, cvv, "")
                            showCardForm = false
                        },
                    )
                }

                Button(
                    onClick = { confirmErase = true },
                    enabled = state.busy == null,
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.error,
                        contentColor = MaterialTheme.colorScheme.onError,
                    ),
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("Erase everything") }
            }
        }

        state.busy?.let { BusyOverlay(it) }

        state.message?.let {
            StatusCard(
                title = "Done",
                body = it,
                container = MaterialTheme.colorScheme.surfaceVariant,
                onContainer = MaterialTheme.colorScheme.onSurfaceVariant,
            ) {
                TextButton(onClick = { viewModel.dismissMessage() }) { Text("Dismiss") }
            }
        }

        if (state.error != null && state.customer != null) {
            StatusCard(
                title = "That did not work",
                body = state.error!!,
                container = MaterialTheme.colorScheme.errorContainer,
                onContainer = MaterialTheme.colorScheme.onErrorContainer,
            )
        }

        OutlinedButton(onClick = onSettings, modifier = Modifier.fillMaxWidth()) {
            Text("Server settings")
        }
    }

    if (confirmErase) {
        AlertDialog(
            onDismissRequest = { confirmErase = false },
            title = { Text("Erase everything?") },
            text = {
                Text(
                    "This destroys the encryption key for your data. Your palm " +
                        "template and card token become permanently unreadable -- " +
                        "including in our backups. It cannot be undone, and you " +
                        "would have to set up again from scratch."
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmErase = false
                    viewModel.eraseAccount(onErased)
                }) { Text("Erase permanently") }
            },
            dismissButton = {
                TextButton(onClick = { confirmErase = false }) { Text("Keep my account") }
            },
        )
    }
}

@Composable
private fun CardForm(
    busy: Boolean,
    onSubmit: (String, String, String, String) -> Unit,
) {
    var number by remember { mutableStateOf("") }
    var month by remember { mutableStateOf("") }
    var year by remember { mutableStateOf("") }
    var cvv by remember { mutableStateOf("") }

    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(
            "Your palm scan stays as it is -- only the card changes.",
            style = MaterialTheme.typography.bodyMedium,
        )
        OutlinedTextField(
            value = number,
            onValueChange = { number = it.filter(Char::isDigit).take(19) },
            label = { Text("New card number") },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
            modifier = Modifier.fillMaxWidth(),
        )
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlinedTextField(
                value = month,
                onValueChange = { month = it.filter(Char::isDigit).take(2) },
                label = { Text("MM") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                modifier = Modifier.weight(1f),
            )
            OutlinedTextField(
                value = year,
                onValueChange = { year = it.filter(Char::isDigit).take(4) },
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
        Button(
            onClick = { onSubmit(number, month, year, cvv) },
            enabled = !busy && number.length >= 12 && month.isNotBlank() &&
                year.isNotBlank() && cvv.length >= 3,
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Save new card") }
    }
}
