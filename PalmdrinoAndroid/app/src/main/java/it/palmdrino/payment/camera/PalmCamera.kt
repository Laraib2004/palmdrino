package it.palmdrino.payment.camera

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine

/**
 * Palm capture via CameraX.
 *
 * Frames are captured straight to an in-memory JPEG and handed to the caller.
 * Nothing is written to device storage, and no bitmap is cached beyond the
 * request that uses it: a palm image on disk is biometric data at rest on a
 * device with no key management, which is exactly what the server-side design
 * is built to avoid.
 */
class PalmCameraController {

    private var imageCapture: ImageCapture? = null

    fun bind(context: Context, previewView: PreviewView, lifecycleOwner: androidx.lifecycle.LifecycleOwner) {
        val providerFuture = ProcessCameraProvider.getInstance(context)
        providerFuture.addListener({
            val provider = providerFuture.get()

            val preview = Preview.Builder().build().also {
                it.surfaceProvider = previewView.surfaceProvider
            }
            val capture = ImageCapture.Builder()
                // Latency matters less than image quality here: a soft frame
                // fails the server quality gate and costs the user a retry,
                // which is slower than capturing carefully once.
                .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
                .build()

            provider.unbindAll()
            provider.bindToLifecycle(
                lifecycleOwner,
                CameraSelector.DEFAULT_BACK_CAMERA,
                preview,
                capture,
            )
            imageCapture = capture
        }, ContextCompat.getMainExecutor(context))
    }

    /** Capture one frame as JPEG bytes. */
    suspend fun capture(context: Context): ByteArray {
        val capture = imageCapture ?: error("camera is not ready yet")
        return suspendCoroutine { continuation ->
            capture.takePicture(
                ContextCompat.getMainExecutor(context),
                object : ImageCapture.OnImageCapturedCallback() {
                    override fun onCaptureSuccess(image: ImageProxy) {
                        try {
                            continuation.resume(image.toJpeg())
                        } catch (error: Exception) {
                            continuation.resumeWithException(error)
                        } finally {
                            image.close()
                        }
                    }

                    override fun onError(exception: ImageCaptureException) {
                        continuation.resumeWithException(exception)
                    }
                },
            )
        }
    }
}

/**
 * Convert a captured frame to JPEG, applying the sensor rotation.
 *
 * Rotation matters: the server normalises ROI rotation from the finger-valley
 * line, but it still has to find a hand first, and skin segmentation plus
 * contour analysis behave better on an upright frame.
 */
private fun ImageProxy.toJpeg(quality: Int = 92): ByteArray {
    val buffer = planes[0].buffer
    val bytes = ByteArray(buffer.remaining()).also { buffer.get(it) }

    val rotation = imageInfo.rotationDegrees
    if (rotation == 0) return bytes

    val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: return bytes
    val rotated = Bitmap.createBitmap(
        bitmap,
        0,
        0,
        bitmap.width,
        bitmap.height,
        Matrix().apply { postRotate(rotation.toFloat()) },
        true,
    )
    return ByteArrayOutputStream().use { stream ->
        rotated.compress(Bitmap.CompressFormat.JPEG, quality, stream)
        stream.toByteArray()
    }
}

/** Live camera preview bound to the composition's lifecycle. */
@Composable
fun PalmCameraPreview(
    controller: PalmCameraController,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val previewView = remember {
        PreviewView(context).apply {
            scaleType = PreviewView.ScaleType.FILL_CENTER
        }
    }

    AndroidView(
        factory = {
            controller.bind(context, previewView, lifecycleOwner)
            previewView
        },
        modifier = modifier,
    )
}
