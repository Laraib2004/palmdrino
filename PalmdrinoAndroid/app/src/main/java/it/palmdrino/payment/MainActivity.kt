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
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import it.palmdrino.payment.data.ApiSettings
import it.palmdrino.payment.ui.EnrollScreen
import it.palmdrino.payment.ui.HomeScreen
import it.palmdrino.payment.ui.PalmdrinoTheme
import it.palmdrino.payment.ui.PayScreen
import it.palmdrino.payment.ui.SettingsScreen

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
                    PalmdrinoApp()
                }
            }
        }
    }
}

private object Routes {
    const val HOME = "home"
    const val ENROLL = "enroll"
    const val PAY = "pay"
    const val SETTINGS = "settings"
}

@Composable
fun PalmdrinoApp() {
    val context = LocalContext.current
    val navController = rememberNavController()
    val settings = remember { ApiSettings(context) }

    NavHost(
        navController = navController,
        startDestination = Routes.HOME,
        // Keep content clear of the status and navigation bars.
        modifier = Modifier.fillMaxSize().systemBarsPadding(),
    ) {
        composable(Routes.HOME) {
            HomeScreen(
                settings = settings,
                onEnroll = { navController.navigate(Routes.ENROLL) },
                onPay = { navController.navigate(Routes.PAY) },
                onSettings = { navController.navigate(Routes.SETTINGS) },
            )
        }
        composable(Routes.ENROLL) {
            EnrollScreen(settings = settings, onDone = { navController.popBackStack() })
        }
        composable(Routes.PAY) {
            PayScreen(settings = settings, onDone = { navController.popBackStack() })
        }
        composable(Routes.SETTINGS) {
            SettingsScreen(settings = settings, onDone = { navController.popBackStack() })
        }
    }
}
