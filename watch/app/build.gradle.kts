import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "care.ira.watch"
    compileSdk = 35

    defaultConfig {
        applicationId = "care.ira.watch"
        minSdk = 30          // Wear OS 3: Galaxy Watch 4 and later
        targetSdk = 35
        // versionCode counts up per store upload; set IRA_WATCH_VERSION_CODE in the release build.
        versionCode = (System.getenv("IRA_WATCH_VERSION_CODE") ?: "1").toInt()
        versionName = "0.1.0"
        // The backend the watch talks to. Set per build; the default is a
        // local development server reached from the emulator.
        buildConfigField("String", "CARE_API_BASE_URL", "\"http://10.0.2.2:8000\"")
    }

    // Release signing: a keystore the owner holds, named by environment variables
    // or a keystore.properties file next to this module. Neither is committed.
    // Without them the release build is unsigned (fine for CI; not installable).
    val keystoreProps = Properties().apply {
        val f = rootProject.file("keystore.properties")
        if (f.exists()) f.inputStream().use { load(it) }
    }
    fun secret(name: String): String? = System.getenv(name) ?: keystoreProps.getProperty(name)
    signingConfigs {
        create("release") {
            val path = secret("IRA_WATCH_KEYSTORE")
            if (path != null) {
                storeFile = file(path)
                storePassword = secret("IRA_WATCH_KEYSTORE_PASSWORD")
                keyAlias = secret("IRA_WATCH_KEY_ALIAS")
                keyPassword = secret("IRA_WATCH_KEY_PASSWORD")
            }
        }
    }
    // Two platforms, one app (phase 8). `wearos`: Google's Wear OS on development
    // boards and shop watches, readings through Health Services. `aosp`: our own
    // watch on plain Android (AOSP) with no Google service on it: readings straight
    // from the Android sensor framework (docs/hardware/SENSOR_HAL.md), speech
    // from whatever engine the build installs, push over our own server channel.
    // CI builds both and fails if the aosp flavour pulls in a Google dependency.
    flavorDimensions += "platform"
    productFlavors {
        create("wearos") {
            dimension = "platform"
            isDefault = true
            buildConfigField("String", "PLATFORM", "\"wearos\"")
        }
        create("aosp") {
            dimension = "platform"
            buildConfigField("String", "PLATFORM", "\"aosp\"")
        }
    }
    // Simulated sensors (a scripted driver, no hardware) for the emulator, CI and the
    // presenter: IRA_WATCH_SIMULATED_SENSORS=1 at build time. Never on by default.
    defaultConfig.buildConfigField("boolean", "SIMULATED_SENSORS", (System.getenv("IRA_WATCH_SIMULATED_SENSORS") == "1").toString())
    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            if (secret("IRA_WATCH_KEYSTORE") != null) signingConfig = signingConfigs.getByName("release")
            // The production backend, set at build time; never a default that points at a developer's machine.
            buildConfigField("String", "CARE_API_BASE_URL", "\"${secret("IRA_CARE_API_BASE_URL") ?: "https://care.example.invalid"}\"")
        }
        debug {
            applicationIdSuffix = ".debug"
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }
    androidResources {
        // The model is memory-mapped from the assets: it must not be compressed.
        noCompress += "tflite"
    }
    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.12.01")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.wear.compose:compose-material:1.4.0")
    implementation("androidx.wear.compose:compose-foundation:1.4.0")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-service:2.8.7")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.work:work-runtime-ktx:2.10.0")
    // Wear OS only: Health Services needs Google's Health Services app on the watch.
    "wearosImplementation"("androidx.health:health-services-client:1.0.0")
    "wearosImplementation"("com.google.guava:guava:33.3.1-android")
    "wearosImplementation"("org.jetbrains.kotlinx:kotlinx-coroutines-guava:1.9.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // The trained fall model (round 3, phase 5) runs through TensorFlow Lite; the rule detector is the fallback.
    implementation("org.tensorflow:tensorflow-lite:2.16.1")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
}
