package uk.nktnet.webviewkiosk.utils

import android.app.Activity
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import uk.nktnet.webviewkiosk.managers.MqttManager
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.lang.ref.WeakReference
import java.util.Locale

object QuonixTransferManager {
    private const val SCREENSHOT_MAX_PIXELS = 12_000_000L
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var activityRef: WeakReference<Activity>? = null

    fun registerActivity(activity: Activity) {
        activityRef = WeakReference(activity)
    }

    fun unregisterActivity(activity: Activity) {
        if (activityRef?.get() === activity) {
            activityRef = null
        }
    }

    fun screenshot() {
        val activity = activityRef?.get()
        if (activity == null) {
            MqttManager.publishTransferError("screenshot", "MainActivity is not available.")
            return
        }

        scope.launch {
            try {
                val bitmap = withContext(Dispatchers.Main.immediate) {
                    val view = activity.window.decorView
                    val width = view.width
                    val height = view.height
                    if (width <= 0 || height <= 0 || width.toLong() * height > SCREENSHOT_MAX_PIXELS) {
                        null
                    } else {
                        Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888).also {
                            view.draw(Canvas(it))
                        }
                    }
                }

                if (bitmap == null) {
                    MqttManager.publishTransferError("screenshot", "Screen is not ready or too large.")
                    return@launch
                }

                val output = ByteArrayOutputStream()
                bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)
                bitmap.recycle()
                val bytes = output.toByteArray()
                MqttManager.publishFileTransfer(
                    fileName = "quonix-screenshot-${System.currentTimeMillis()}.png",
                    mimeType = "image/png",
                    category = "screenshot",
                    size = bytes.size.toLong(),
                    openStream = { bytes.inputStream() },
                )
            } catch (e: Exception) {
                MqttManager.publishTransferError("screenshot", e.message ?: e.toString())
            }
        }
    }

    fun exportMedia(context: Context, includeImages: Boolean, includeVideos: Boolean) {
        scope.launch {
            var exported = 0
            try {
                if (includeImages) {
                    exported += exportCollection(
                        context,
                        MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                        "image"
                    )
                }
                if (includeVideos) {
                    exported += exportCollection(
                        context,
                        MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
                        "video"
                    )
                }
                if (exported == 0) {
                    MqttManager.publishTransferError("export_media", "Keine zugänglichen Bilder/Videos gefunden.")
                }
            } catch (e: SecurityException) {
                MqttManager.publishTransferError(
                    "export_media",
                    "Medienberechtigung fehlt. Auf Android 13+ READ_MEDIA_IMAGES/READ_MEDIA_VIDEO erlauben."
                )
            } catch (e: Exception) {
                MqttManager.publishTransferError("export_media", e.message ?: e.toString())
            }
        }
    }

    private suspend fun exportCollection(
        context: Context,
        collection: Uri,
        category: String,
    ): Int {
        val projection = arrayOf(
            MediaStore.MediaColumns._ID,
            MediaStore.MediaColumns.DISPLAY_NAME,
            MediaStore.MediaColumns.MIME_TYPE,
            MediaStore.MediaColumns.SIZE,
        )
        var count = 0
        context.contentResolver.query(
            collection,
            projection,
            null,
            null,
            MediaStore.MediaColumns.DATE_ADDED + " DESC"
        )?.use { cursor ->
            val idIndex = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns._ID)
            val nameIndex = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DISPLAY_NAME)
            val mimeIndex = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.MIME_TYPE)
            val sizeIndex = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.SIZE)

            while (cursor.moveToNext()) {
                val id = cursor.getLong(idIndex)
                val name = cursor.getString(nameIndex)
                    ?.takeIf { it.isNotBlank() }
                    ?: "${category}-${id}"
                val mime = cursor.getString(mimeIndex)
                    ?.takeIf { it.isNotBlank() }
                    ?: if (category == "image") "image/*" else "video/*"
                val size = cursor.getLong(sizeIndex).coerceAtLeast(0L)
                val uri = Uri.withAppendedPath(collection, id.toString())

                if (size > 0L) {
                    val safeName = name.replace(Regex("[\\/:*?"<>|]"), "_")
                    MqttManager.publishFileTransfer(
                        fileName = safeName,
                        mimeType = mime,
                        category = category,
                        size = size,
                        openStream = {
                            context.contentResolver.openInputStream(uri)
                        },
                    )
                    count++
                }
            }
        }
        return count
    }
}
