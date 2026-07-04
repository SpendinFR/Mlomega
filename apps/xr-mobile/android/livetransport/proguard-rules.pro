# MLOmega V19 — E24 — ProGuard/R8 rules for the LiveTransport library.

# Keep the whole native WebRTC surface: libwebrtc calls back into these classes
# by name over JNI, so obfuscation/stripping breaks the binding.
-keep class org.webrtc.** { *; }
-keepclassmembers class org.webrtc.** { *; }
-dontwarn org.webrtc.**

# Keep the public plugin API called from Unity via reflection (AndroidJavaObject).
-keep class com.mlomega.xr.livetransport.** { public *; }
-keepclassmembers class com.mlomega.xr.livetransport.** {
    public <methods>;
    public <fields>;
}

# OkHttp / Okio (standard consumer rules).
-dontwarn okhttp3.**
-dontwarn okio.**
-dontwarn org.conscrypt.**
