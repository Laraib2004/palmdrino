plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "it.palmdrino.payment"
    compileSdk = 35

    defaultConfig {
        applicationId = "it.palmdrino.payment"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        // Default API base URL. Overridable at runtime from the Settings
        // screen, because the emulator, a physical device on Wi-Fi and a
        // hosted deployment all need different hosts. 10.0.2.2 is how the
        // Android emulator reaches the host machine's localhost.
        buildConfigField("String", "DEFAULT_API_BASE_URL", "\"http://10.0.2.2:8000/\"")
    }

    // PD-27: the customer app and the merchant terminal are different products
    // for different people. Separating them as flavours means a customer build
    // physically cannot contain the payment-taking screen, rather than merely
    // hiding it -- and the two can be signed and distributed independently.
    flavorDimensions += "surface"
    productFlavors {
        create("customer") {
            dimension = "surface"
            applicationIdSuffix = ".customer"
            versionNameSuffix = "-customer"
            resValue("string", "app_name", "Palmdrino")
        }
        create("terminal") {
            dimension = "surface"
            applicationIdSuffix = ".terminal"
            versionNameSuffix = "-terminal"
            resValue("string", "app_name", "Palmdrino Terminal")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }
    kotlinOptions {
        jvmTarget = "11"
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.androidx.security.crypto)

    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.material.icons)
    implementation(libs.androidx.navigation.compose)
    debugImplementation(libs.androidx.compose.ui.tooling)

    implementation(libs.androidx.camera.core)
    implementation(libs.androidx.camera.camera2)
    implementation(libs.androidx.camera.lifecycle)
    implementation(libs.androidx.camera.view)

    implementation(libs.retrofit)
    implementation(libs.retrofit.moshi)
    implementation(libs.moshi.kotlin)
    implementation(libs.okhttp)
    implementation(libs.okhttp.logging)

    testImplementation(libs.junit)
    androidTestImplementation(libs.androidx.junit)
    androidTestImplementation(libs.androidx.espresso.core)
}
