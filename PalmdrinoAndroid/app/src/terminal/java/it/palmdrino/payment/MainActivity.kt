package it.palmdrino.payment

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import it.palmdrino.payment.data.ApiSettings
import it.palmdrino.payment.data.PalmdrinoClient
import it.palmdrino.payment.data.SecureStore
import it.palmdrino.payment.ui.PalmdrinoTheme
import it.palmdrino.payment.ui.terminal.PayScreen
import it.palmdrino.payment.ui.terminal.TerminalSettingsScreen

/**
 * The merchant terminal (PD-27).
 *
 * A separate app from the customer build, for a separate person. It holds a
 * terminal grant, which can take payments and can never read a customer
 * account (D8).
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        setContent {
            PalmdrinoTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    TerminalApp()
                }
            }
        }
    }
}

private object Routes {
    const val PAY = "pay"
    const val SETTINGS = "settings"
}

@Composable
fun TerminalApp() {
    val context = LocalContext.current
    val navController = rememberNavController()
    val settings = remember { ApiSettings(context) }
    val store = remember { SecureStore(context) }

    // The terminal key is stored encrypted (PD-15) and loaded once at startup,
    // rather than being re-entered by staff every shift.
    LaunchedEffect(Unit) { PalmdrinoClient.apiKey = store.terminalApiKey }

    NavHost(
        navController = navController,
        startDestination = Routes.PAY,
        modifier = Modifier.fillMaxSize().systemBarsPadding(),
    ) {
        composable(Routes.PAY) {
            PayScreen(
                settings = settings,
                onSettings = { navController.navigate(Routes.SETTINGS) },
            )
        }
        composable(Routes.SETTINGS) {
            TerminalSettingsScreen(onDone = { navController.popBackStack() })
        }
    }
}
