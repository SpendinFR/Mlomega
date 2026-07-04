# MLOmega V19 — E24 — Consumer ProGuard rules applied to the app that depends on
# this library (the Unity XR app). These must survive the app's own R8 pass.

# libwebrtc JNI surface must be kept intact in the final app.
-keep class org.webrtc.** { *; }
-dontwarn org.webrtc.**

# Public plugin API invoked from Unity via reflection.
-keep class com.mlomega.xr.livetransport.LiveTransportPlugin { public *; }
-keep interface com.mlomega.xr.livetransport.LiveTransportCallbacks { *; }
-keep interface com.mlomega.xr.livetransport.VideoFrameFeeder { *; }
