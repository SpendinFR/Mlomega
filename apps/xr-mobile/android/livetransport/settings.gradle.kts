// MLOmega V19 — E24 — standalone Gradle library for the mobile live transport.
//
// Kept self-contained (its own settings) so it can be built in isolation for CI
// or vendored as an .aar into the Unity project (Assets/Plugins/Android). When
// consumed inside a larger Unity/Gradle build, add ":livetransport" via that
// build's settings and drop this file's includeBuild usage.

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
    }
}

rootProject.name = "livetransport"
