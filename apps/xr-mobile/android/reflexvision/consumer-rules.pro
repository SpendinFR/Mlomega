# MLOmega V19 — E26 — Consumer ProGuard rules applied to the app that depends on
# this library (the Unity XR app). These must survive the app's own R8 pass.

# MediaPipe Tasks JNI surface must be kept intact in the final app.
-keep class com.google.mediapipe.** { *; }
-dontwarn com.google.mediapipe.**

# sherpa-onnx JNI surface (native methods bound by name).
-keep class com.k2fsa.sherpa.onnx.** { *; }
-dontwarn com.k2fsa.sherpa.onnx.**

# Public plugin API invoked from Unity via reflection.
-keep class com.mlomega.xr.reflexvision.GesturePipeline { public *; }
-keep class com.mlomega.xr.reflexvision.AsrKwsService { public *; }
-keep interface com.mlomega.xr.reflexvision.GestureCallbacks { *; }
-keep interface com.mlomega.xr.reflexvision.AsrKwsCallbacks { *; }
