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
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import it.palmdrino.payment.ui.PalmdrinoTheme
import it.palmdrino.payment.ui.customer.AccountScreen
import it.palmdrino.payment.ui.customer.CustomerViewModel
import it.palmdrino.payment.ui.customer.CustomerSettingsScreen
import it.palmdrino.payment.ui.customer.SetupScreen

/**
 * The customer app (PD-26).
 *
 * This build contains no way to take a payment. That lives in the terminal
 * flavour, which is a separate app for a different person (PD-27) -- the
 * separation is structural rather than a hidden menu.
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
                    CustomerApp()
                }
            }
        }
    }
}

private object Routes {
    const val SETUP = "setup"
    const val ACCOUNT = "account"
    const val SETTINGS = "settings"
}

@Composable
fun CustomerApp() {
    val navController = rememberNavController()
    val viewModel: CustomerViewModel = viewModel()

    // Whether this device already holds a credential decides the landing
    // screen: an enrolled phone should open on the account, not on a setup
    // flow it has already completed.
    val start = if (viewModel.isEnrolled) Routes.ACCOUNT else Routes.SETUP

    NavHost(
        navController = navController,
        startDestination = start,
        modifier = Modifier.fillMaxSize().systemBarsPadding(),
    ) {
        composable(Routes.SETUP) {
            SetupScreen(
                viewModel = viewModel,
                onDone = {
                    navController.navigate(Routes.ACCOUNT) {
                        popUpTo(Routes.SETUP) { inclusive = true }
                    }
                },
                onSettings = { navController.navigate(Routes.SETTINGS) },
            )
        }
        composable(Routes.ACCOUNT) {
            AccountScreen(
                viewModel = viewModel,
                onErased = {
                    navController.navigate(Routes.SETUP) {
                        popUpTo(Routes.ACCOUNT) { inclusive = true }
                    }
                },
                onSettings = { navController.navigate(Routes.SETTINGS) },
            )
        }
        composable(Routes.SETTINGS) {
            CustomerSettingsScreen(onDone = { navController.popBackStack() })
        }
    }
}
