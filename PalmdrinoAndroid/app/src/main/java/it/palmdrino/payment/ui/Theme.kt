package it.palmdrino.payment.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

private val Teal = Color(0xFF00695C)
private val TealLight = Color(0xFF4DB6AC)
private val Amber = Color(0xFFFFB300)
private val Ink = Color(0xFF11201E)

private val LightColors = lightColorScheme(
    primary = Teal,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFB2DFDB),
    onPrimaryContainer = Ink,
    secondary = Amber,
    onSecondary = Ink,
    secondaryContainer = Color(0xFFFFE8B2),
    onSecondaryContainer = Ink,
    background = Color(0xFFF6F8F7),
    onBackground = Ink,
    surface = Color.White,
    onSurface = Ink,
    surfaceVariant = Color(0xFFE6EDEB),
    onSurfaceVariant = Color(0xFF3A4442),
    error = Color(0xFFB3261E),
    errorContainer = Color(0xFFF9DEDC),
    onErrorContainer = Color(0xFF410E0B),
)

private val DarkColors = darkColorScheme(
    primary = TealLight,
    onPrimary = Ink,
    primaryContainer = Color(0xFF00504A),
    onPrimaryContainer = Color(0xFFB2DFDB),
    secondary = Amber,
    onSecondary = Ink,
    secondaryContainer = Color(0xFF5C4400),
    onSecondaryContainer = Color(0xFFFFE8B2),
    background = Color(0xFF0E1513),
    onBackground = Color(0xFFE0E4E3),
    surface = Color(0xFF16201E),
    onSurface = Color(0xFFE0E4E3),
    surfaceVariant = Color(0xFF2A3634),
    onSurfaceVariant = Color(0xFFC5CDCB),
    error = Color(0xFFF2B8B5),
    errorContainer = Color(0xFF8C1D18),
    onErrorContainer = Color(0xFFF9DEDC),
)

private val PalmdrinoTypography = Typography(
    headlineMedium = TextStyle(fontSize = 28.sp, fontWeight = FontWeight.SemiBold),
    titleLarge = TextStyle(fontSize = 20.sp, fontWeight = FontWeight.SemiBold),
    bodyLarge = TextStyle(fontSize = 16.sp),
    bodyMedium = TextStyle(fontSize = 14.sp),
    labelMedium = TextStyle(fontSize = 12.sp, fontWeight = FontWeight.Medium),
)

/**
 * App theme.
 *
 * System bar appearance is handled by `enableEdgeToEdge()` in the activity
 * rather than by writing to the deprecated `statusBarColor`.
 */
@Composable
fun PalmdrinoTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkColors else LightColors,
        typography = PalmdrinoTypography,
        content = content,
    )
}
