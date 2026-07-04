// MLOmega V19 — E24 — LiveTransport Android library (GetStream webrtc-android).
//
// Produces an .aar consumed by the Unity XR app (Assets/Plugins/Android) and
// driven from C# via LiveTransportBridge.cs (AndroidJavaObject). Pure library:
// no Activity, no UI. The WebRTC binding is GetStream's precompiled libwebrtc
// (Apache-2.0), pinned below.

plugins {
    id("com.android.library") version "8.5.2"
    id("org.jetbrains.kotlin.android") version "1.9.24"
}

android {
    namespace = "com.mlomega.xr.livetransport"
    compileSdk = 34

    defaultConfig {
        // API-21+ is the GetStream stream-webrtc-android floor. XREAL/S25 target
        // is well above this; 26 gives us stable Camera2/SurfaceTexture behaviour.
        minSdk = 26
        targetSdk = 34

        consumerProguardFiles("consumer-rules.pro")
    }

    buildTypes {
        release {
            isMinifyEnabled = false // the app (Unity) controls final shrinking
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    // Unity vendors the .aar; keep it lean and reproducible.
    packaging {
        resources {
            excludes += setOf("META-INF/*.kotlin_module")
        }
    }
}

dependencies {
    // GetStream webrtc-android — precompiled libwebrtc (video H.264 + Opus +
    // DataChannel). Pinned to the latest stable release 1.3.10 (verified on
    // https://github.com/GetStream/webrtc-android/releases, 2026-07-04). The
    // classes live in the standard `org.webrtc` package, so this is a drop-in
    // libwebrtc binding — the plugin code is portable if the binding is swapped.
    // Roadmap risk (handoff §4): Stream owns the release cadence; the version is
    // frozen here at the first reproducible build (ADR docs/DECISIONS.md §E24).
    implementation("io.getstream:stream-webrtc-android:1.3.10")

    // OkHttp for the signaling POST /webrtc/offer round-trip.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // Kotlin coroutines for the reconnect/backoff loop and stats polling.
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    implementation("androidx.annotation:annotation:1.8.0")

    // Pure-JVM unit tests for the SDP munging logic (no device required).
    testImplementation("junit:junit:4.13.2")
}
