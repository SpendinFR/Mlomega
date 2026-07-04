// MLOmega V19 — E26 — ReflexVision Android library.
//
// The on-device Ultra-Live sensing back-ends (handoff §3.2: NO LLM / NO VLM on
// this path — small specialised calculators only, < 100 ms on the device):
//   * GesturePipeline — MediaPipe Tasks Vision HandLandmarker + GestureRecognizer
//     in LIVE_STREAM mode (pinch → continuous zoom, held open palm → menu,
//     lateral swipe → hide UI).
//   * AsrKwsService — sherpa-onnx VAD + streaming zipformer ASR (FR/EN) +
//     KeywordSpotter (configurable wake word).
//
// Produces an .aar consumed by the Unity XR app (Assets/Plugins/Android) and
// driven from C# via GestureBridge.cs / AsrBridge.cs (AndroidJavaObject). Pure
// library: no Activity, no UI. Same conventions as the E24 `livetransport`
// module (pinned versions, KDoc, JNI-friendly public surface).
//
// This module cannot be compiled in the authoring environment (no Android SDK);
// it is written against the pinned MediaPipe / sherpa-onnx APIs below and the
// real compile/run belongs to the S25 validation gate (ADR docs/DECISIONS §E26).

plugins {
    id("com.android.library") version "8.5.2"
    id("org.jetbrains.kotlin.android") version "1.9.24"
}

android {
    namespace = "com.mlomega.xr.reflexvision"
    compileSdk = 34

    defaultConfig {
        // MediaPipe Tasks Vision requires API 24+; sherpa-onnx runs on 21+.
        // 26 matches the livetransport floor (stable Camera2/AudioRecord).
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
    // MediaPipe Tasks Vision — HandLandmarker + GestureRecognizer, LIVE_STREAM.
    // Pinned to the last stable release verified on Maven Central (0.10.29,
    // 2025-09). Apache-2.0. The `.task` bundle models are downloaded to app
    // storage (see README), never committed. ADR docs/DECISIONS.md §E26.
    implementation("com.google.mediapipe:tasks-vision:0.10.29")

    // sherpa-onnx Android AAR (JNI) — VAD + streaming zipformer ASR + KeywordSpotter.
    // Apache-2.0. Consumed via JitPack at the pinned tag 1.12.10 (released
    // 2025-08-25). The static-AAR from the GitHub release can be vendored instead
    // (flatDir) if JitPack is unavailable on a LAN-only build — see README.
    implementation("com.github.k2-fsa:sherpa-onnx-android:1.12.10")

    // Kotlin coroutines for the audio pump + reconnect-free streaming loop.
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    implementation("androidx.annotation:annotation:1.8.0")

    // Pure-JVM unit tests for the gesture state machine / config encoding
    // (no device, no native models required).
    testImplementation("junit:junit:4.13.2")
}
