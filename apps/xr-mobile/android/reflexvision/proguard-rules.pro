# MLOmega V19 — E26 — ProGuard/R8 rules for the ReflexVision library.

# Keep the whole MediaPipe Tasks surface: it calls back into these classes by
# name over JNI, so obfuscation/stripping breaks the binding.
-keep class com.google.mediapipe.** { *; }
-keepclassmembers class com.google.mediapipe.** { *; }
-dontwarn com.google.mediapipe.**

# Keep the sherpa-onnx JNI surface (native methods + result POJOs read by name).
-keep class com.k2fsa.sherpa.onnx.** { *; }
-keepclassmembers class com.k2fsa.sherpa.onnx.** { *; }
-dontwarn com.k2fsa.sherpa.onnx.**

# Keep the public plugin API called from Unity via reflection (AndroidJavaObject).
-keep class com.mlomega.xr.reflexvision.** { public *; }
-keepclassmembers class com.mlomega.xr.reflexvision.** {
    public <methods>;
    public <fields>;
}
