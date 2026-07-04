// MLOmega V19 — E26 — standalone Gradle library for the reflex vision/audio
// back-ends (MediaPipe gestures + sherpa-onnx ASR/KWS).
//
// Kept self-contained (its own settings) so it can be built in isolation for CI
// or vendored as an .aar into the Unity project (Assets/Plugins/Android). When
// consumed inside a larger Unity/Gradle build, add ":reflexvision" via that
// build's settings.
//
// JitPack is added for the sherpa-onnx Android AAR (com.github.k2-fsa:
// sherpa-onnx-android). If a LAN-only build cannot reach JitPack, vendor the
// static AAR from the GitHub release and swap the dependency to a flatDir entry
// (see README + build.gradle.kts comments).

pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.PREFER_SETTINGS)
    repositories {
        google()
        mavenCentral()
        maven { url = uri("https://jitpack.io") }
    }
}

rootProject.name = "reflexvision"
