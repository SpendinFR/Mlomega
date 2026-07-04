// MLOmega V19 — E22 / Gate G1
// Real device adapter built on XREAL SDK 3.1.0 for Unity 6 LTS.
//
// Doc references (https://docs.xreal.com/):
//   - RGB Eye capture uses RGBCameraTexture (namespace Unity.XR.XREAL, the SDK 3.x
//     successor to NRSDK's NRRGBCamTexture). The camera delivers YUV_420_888 only;
//     GetYUVFormatTextures() returns {Y, U, V} which we convert to RGB in a shader
//     blit (Hidden/MLOmega/YUV420ToRGB). Play()/Stop() drive the capture lifecycle.
//   - Only the "Eye" accessory on XREAL One series exposes this feature; if the
//     Eye is absent or unsupported on the unit, capture stays inactive and the app
//     falls back to pose-only (plan B — see README.md).
//   - 6DoF head pose is read from the XR head transform. With XR Plug-in Management +
//     the XREAL provider, Unity's main XR camera transform already carries the fused
//     6DoF pose, so we read it directly and mark tracking from the SDK session state.
//
// This file references XREAL SDK types under the XREAL_SDK_PRESENT define. Because
// the proprietary SDK is not committed, the define is off in a fresh checkout and a
// small reflection-free guard keeps the project compiling; once the tarball is
// installed the developer enables XREAL_SDK_PRESENT (Player Settings > Scripting
// Define Symbols, Android) and the real path activates. See README.md.
using System;
using System.Diagnostics;
using UnityEngine;
#if XREAL_SDK_PRESENT
using Unity.XR.XREAL;
#endif

namespace MLOmega.XR.Core
{
    /// <summary>
    /// Production adapter bound to the XREAL SDK. When XREAL_SDK_PRESENT is not
    /// defined (no tarball installed yet) it reports <see cref="DeviceConnectionState.Disconnected"/>
    /// and yields no frames, so the rest of the app still loads.
    /// </summary>
    public sealed class XrealDeviceAdapter : IXRDeviceAdapter, IDisposable
    {
        private const string YuvShaderName = "Hidden/MLOmega/YUV420ToRGB";

        private DeviceConnectionState _state = DeviceConnectionState.Unknown;
        private bool _eyeActive;
        private long _frameId;
        private readonly Stopwatch _clock = Stopwatch.StartNew();

        private Transform _headTransform;
        private Material _yuvMaterial;
        private RenderTexture _rgbTarget;
        private long _lastNativeTimestamp = -1;

#if XREAL_SDK_PRESENT
        private RGBCameraTexture _rgbCamera;
#endif

        public DeviceConnectionState ConnectionState => _state;
        public string DeviceName { get; private set; } = "XREAL (uninitialized)";
        public bool IsEyeActive => _eyeActive;
        public bool IsStereo => true;
        public string FrameSource => ContractDefaults.FrameSource.XrealEye;

        public event Action<DeviceConnectionState> ConnectionStateChanged;

        private static long NanosPerTick => 1_000_000_000L / Stopwatch.Frequency;

        private void SetState(DeviceConnectionState next)
        {
            if (_state == next)
            {
                return;
            }
            _state = next;
            try
            {
                ConnectionStateChanged?.Invoke(next);
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[XrealDeviceAdapter] state handler threw: {ex}");
            }
        }

        public void Initialize()
        {
#if XREAL_SDK_PRESENT
            try
            {
                SetState(DeviceConnectionState.Connecting);

                // The XR head/main camera transform carries the fused 6DoF pose once
                // the XREAL loader is active. Prefer the tagged MainCamera; fall back
                // to Camera.main.
                Camera head = Camera.main;
                if (head == null)
                {
                    throw new InvalidOperationException(
                        "No main camera found; the XREAL XR rig must be present in the scene.");
                }
                _headTransform = head.transform;

                Shader yuv = Shader.Find(YuvShaderName);
                if (yuv == null)
                {
                    throw new InvalidOperationException(
                        $"Shader '{YuvShaderName}' not found. Ensure YUV420ToRGB.shader is in the build.");
                }
                _yuvMaterial = new Material(yuv);

                DeviceName = XREALPlugin.GetDeviceName() ?? "XREAL One";
                SetState(DeviceConnectionState.Connected);
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogError($"[XrealDeviceAdapter] Initialize failed: {ex}");
                SetState(DeviceConnectionState.Error);
                throw;
            }
#else
            DeviceName = "XREAL (SDK not installed)";
            SetState(DeviceConnectionState.Disconnected);
            UnityEngine.Debug.LogWarning(
                "[XrealDeviceAdapter] XREAL_SDK_PRESENT is not defined. Install the SDK tarball " +
                "and add the scripting define to enable real capture (see README.md).");
#endif
        }

        public void StartEyeCapture()
        {
            if (_eyeActive)
            {
                return;
            }
#if XREAL_SDK_PRESENT
            try
            {
                _rgbCamera = new RGBCameraTexture();
                _rgbCamera.Play();
                _eyeActive = true;
                _frameId = 0;
                _lastNativeTimestamp = -1;
                UnityEngine.Debug.Log("[XrealDeviceAdapter] Eye capture started.");
            }
            catch (Exception ex)
            {
                // Eye may be physically absent on this unit (One vs One Pro). Do not
                // crash the gate: keep pose-only and let the overlay show Eye=KO.
                _eyeActive = false;
                _rgbCamera = null;
                UnityEngine.Debug.LogWarning(
                    $"[XrealDeviceAdapter] Eye capture unavailable (plan B pose-only): {ex.Message}");
            }
#else
            UnityEngine.Debug.LogWarning("[XrealDeviceAdapter] StartEyeCapture: SDK not installed.");
#endif
        }

        public void StopEyeCapture()
        {
            if (!_eyeActive)
            {
                return;
            }
            _eyeActive = false;
#if XREAL_SDK_PRESENT
            try
            {
                _rgbCamera?.Stop();
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[XrealDeviceAdapter] StopEyeCapture threw: {ex.Message}");
            }
            finally
            {
                _rgbCamera = null;
            }
#endif
        }

        public PoseSample GetPose()
        {
#if XREAL_SDK_PRESENT
            if (_state != DeviceConnectionState.Connected || _headTransform == null)
            {
                return PoseSample.Untracked;
            }
            bool tracking = XREALPlugin.IsTracking();
            return new PoseSample(_headTransform.position, _headTransform.rotation, tracking);
#else
            return PoseSample.Untracked;
#endif
        }

        public EyeFrame? TryGetLatestFrame()
        {
#if XREAL_SDK_PRESENT
            if (!_eyeActive || _rgbCamera == null)
            {
                return null;
            }
            try
            {
                if (!_rgbCamera.DidUpdateThisFrame)
                {
                    return null;
                }

                // YUV_420_888 -> RGB via shader blit. GetYUVFormatTextures() returns
                // the three planes {Y, U, V}; the material samples all three.
                Texture[] planes = _rgbCamera.GetYUVFormatTextures();
                if (planes == null || planes.Length < 3 || planes[0] == null)
                {
                    return null;
                }

                int w = planes[0].width;
                int h = planes[0].height;
                EnsureTarget(w, h);

                _yuvMaterial.SetTexture("_YTex", planes[0]);
                _yuvMaterial.SetTexture("_UTex", planes[1]);
                _yuvMaterial.SetTexture("_VTex", planes[2]);
                Graphics.Blit(null, _rgbTarget, _yuvMaterial);

                long nativeTs = _rgbCamera.GetFrameTimestampNs();
                if (nativeTs > 0 && nativeTs != _lastNativeTimestamp)
                {
                    _lastNativeTimestamp = nativeTs;
                }
                long monotonicNs = nativeTs > 0 ? nativeTs : _clock.ElapsedTicks * NanosPerTick;

                _frameId++;
                return new EyeFrame(_rgbTarget, _frameId, monotonicNs);
            }
            catch (Exception ex)
            {
                UnityEngine.Debug.LogWarning($"[XrealDeviceAdapter] TryGetLatestFrame threw: {ex.Message}");
                return null;
            }
#else
            return null;
#endif
        }

        private void EnsureTarget(int width, int height)
        {
            if (_rgbTarget != null && _rgbTarget.width == width && _rgbTarget.height == height)
            {
                return;
            }
            if (_rgbTarget != null)
            {
                _rgbTarget.Release();
                UnityEngine.Object.Destroy(_rgbTarget);
            }
            _rgbTarget = new RenderTexture(width, height, 0, RenderTextureFormat.ARGB32)
            {
                name = "XrealEyeRGB",
                wrapMode = TextureWrapMode.Clamp
            };
            _rgbTarget.Create();
        }

        public void Shutdown()
        {
            StopEyeCapture();
            if (_rgbTarget != null)
            {
                _rgbTarget.Release();
                UnityEngine.Object.Destroy(_rgbTarget);
                _rgbTarget = null;
            }
            if (_yuvMaterial != null)
            {
                UnityEngine.Object.Destroy(_yuvMaterial);
                _yuvMaterial = null;
            }
            _headTransform = null;
            SetState(DeviceConnectionState.Disconnected);
        }

        public void Dispose() => Shutdown();
    }
}
