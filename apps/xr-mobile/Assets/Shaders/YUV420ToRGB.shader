// MLOmega V19 — E22 / Gate G1
// Converts XREAL Eye YUV_420_888 (three planes) to RGB in a single blit.
// GetYUVFormatTextures() returns {Y, U, V}; U and V are half-resolution and are
// sampled with the same normalized UV. BT.601 limited-range coefficients.
Shader "Hidden/MLOmega/YUV420ToRGB"
{
    Properties
    {
        _YTex ("Y", 2D) = "black" {}
        _UTex ("U", 2D) = "gray" {}
        _VTex ("V", 2D) = "gray" {}
    }
    SubShader
    {
        Tags { "RenderType" = "Opaque" }
        Cull Off ZWrite Off ZTest Always

        Pass
        {
            HLSLPROGRAM
            #pragma vertex vert
            #pragma fragment frag
            #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Core.hlsl"

            TEXTURE2D(_YTex); SAMPLER(sampler_YTex);
            TEXTURE2D(_UTex); SAMPLER(sampler_UTex);
            TEXTURE2D(_VTex); SAMPLER(sampler_VTex);

            struct Attributes { float4 positionOS : POSITION; float2 uv : TEXCOORD0; };
            struct Varyings   { float4 positionHCS : SV_POSITION; float2 uv : TEXCOORD0; };

            Varyings vert (Attributes IN)
            {
                Varyings OUT;
                OUT.positionHCS = TransformObjectToHClip(IN.positionOS.xyz);
                OUT.uv = IN.uv;
                return OUT;
            }

            half4 frag (Varyings IN) : SV_Target
            {
                float y = SAMPLE_TEXTURE2D(_YTex, sampler_YTex, IN.uv).r;
                float u = SAMPLE_TEXTURE2D(_UTex, sampler_UTex, IN.uv).r - 0.5;
                float v = SAMPLE_TEXTURE2D(_VTex, sampler_VTex, IN.uv).r - 0.5;

                // BT.601, limited range Y in [16/255, 235/255].
                y = (y - 0.0625) * 1.164;
                float r = y + 1.596 * v;
                float g = y - 0.391 * u - 0.813 * v;
                float b = y + 2.018 * u;
                return half4(saturate(float3(r, g, b)), 1.0);
            }
            ENDHLSL
        }
    }
    Fallback Off
}
