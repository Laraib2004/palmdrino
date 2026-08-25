package it.palmdrino.payment.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import it.palmdrino.payment.camera.PalmCameraController
import it.palmdrino.payment.camera.PalmCameraPreview

/** Shows the camera once permission is granted, and asks for it if not. */
@Composable
fun CameraPanel(
    controller: PalmCameraController,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    var granted by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED
        )
    }
    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted = it }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .aspectRatio(3f / 4f)
            .clip(RoundedCornerShape(20.dp)),
        contentAlignment = Alignment.Center,
    ) {
        if (granted) {
            PalmCameraPreview(controller, Modifier.fillMaxSize())
        } else {
            Card(
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.surfaceVariant
                ),
                modifier = Modifier.fillMaxSize(),
            ) {
                Column(
                    modifier = Modifier.fillMaxSize().padding(24.dp),
                    verticalArrangement = Arrangement.Center,
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    Text(
                        "Camera access is needed to scan your palm.",
                        style = MaterialTheme.typography.bodyLarge,
                    )
                    Button(
                        onClick = { launcher.launch(Manifest.permission.CAMERA) },
                        modifier = Modifier.padding(top = 16.dp),
                    ) { Text("Allow camera") }
                }
            }
        }
    }
}

/** A labelled row used throughout the result screens. */
@Composable
fun DetailRow(label: String, value: String) {
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 3.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(label, style = MaterialTheme.typography.bodyMedium)
        Text(
            value,
            style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.SemiBold,
        )
    }
}

@Composable
fun StatusCard(
    title: String,
    body: String,
    container: androidx.compose.ui.graphics.Color,
    onContainer: androidx.compose.ui.graphics.Color,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit = {},
) {
    Card(
        colors = CardDefaults.cardColors(containerColor = container, contentColor = onContainer),
        modifier = modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(16.dp)) {
            Text(title, style = MaterialTheme.typography.titleLarge)
            if (body.isNotBlank()) {
                Text(
                    body,
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.padding(top = 6.dp),
                )
            }
            content()
        }
    }
}

@Composable
fun BusyOverlay(message: String) {
    Box(
        Modifier.fillMaxWidth().padding(16.dp),
        contentAlignment = Alignment.Center,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            CircularProgressIndicator(strokeWidth = 3.dp)
            Text(message, modifier = Modifier.padding(start = 14.dp))
        }
    }
}

/** Format euro cents for display, e.g. 24990 -> "24.90". */
fun formatMinor(amountMinor: Long): String =
    "%,.2f".format(amountMinor / 100.0)

/** Parse a euro amount typed by a cashier into integer cents. */
fun parseEuroToMinor(text: String): Long? {
    val cleaned = text.trim().replace(',', '.')
    if (cleaned.isEmpty()) return null
    val value = cleaned.toDoubleOrNull() ?: return null
    if (value <= 0) return null
    return Math.round(value * 100.0)
}
