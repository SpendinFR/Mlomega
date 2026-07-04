package com.mlomega.xr.reflexvision

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder

/**
 * Minimal foreground service that keeps the microphone capture alive while the
 * XR app is backgrounded (GUIDE_V19_REFERENCE §15.2: mic must survive session
 * lifecycle for wake word + live subtitles). It hosts no logic itself — the
 * [AsrKwsService] owns the AudioRecord loop — it exists solely to hold the
 * `foregroundServiceType="microphone"` slot Android requires for background mic
 * access, with a persistent (privacy-visible) notification.
 *
 * Started/stopped by [AsrKwsService.start]/[stop]. Declared in the manifest.
 */
class MicForegroundService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        ensureChannel(this)
        val notification: Notification = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("MLOmega — listening")
            .setContentText("Wake word + live captions active")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        return START_STICKY
    }

    companion object {
        private const val CHANNEL_ID = "mlomega_reflex_mic"
        private const val NOTIFICATION_ID = 4726

        fun start(context: Context) {
            val i = Intent(context, MicForegroundService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(i)
            } else {
                context.startService(i)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, MicForegroundService::class.java))
        }

        private fun ensureChannel(context: Context) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
            val mgr = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (mgr.getNotificationChannel(CHANNEL_ID) == null) {
                val channel = NotificationChannel(
                    CHANNEL_ID,
                    "MLOmega microphone",
                    NotificationManager.IMPORTANCE_LOW,
                )
                channel.description = "Keeps wake word + live captions running"
                mgr.createNotificationChannel(channel)
            }
        }
    }
}
