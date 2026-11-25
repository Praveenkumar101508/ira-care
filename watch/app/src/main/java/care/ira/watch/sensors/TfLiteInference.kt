package care.ira.watch.sensors

import android.content.Context
import org.tensorflow.lite.Interpreter
import java.io.FileInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.channels.FileChannel
import java.security.MessageDigest

/**
 * The trained fall model through TensorFlow Lite, from the app's assets
 * (`fall_model.tflite`, written by scripts/training/fall_model_v3.py). Built once;
 * `load` returns null when the asset is missing, is not the reviewed v3
 * ([FallModelSpec.mayRun]) or the interpreter cannot start, and the caller then runs
 * the rule detector alone.
 *
 * Which model a build carries (D-077, D-080, D-082): every build, debug and release, and so the
 * IRA CARE OS image, carries fall model v3 from `src/main/assets`, trained only on commercially
 * licensed data (BITS-2 and UCI HAR) and run in "recover" mode by RecoveringFallDetector. No
 * build of any kind carries the SisFall-trained v1. `FallModelReleaseTest` pins this.
 */
class TfLiteInference private constructor(private val interpreter: Interpreter, private val windowSamples: Int) : Inference {
    override fun fallProbability(window: Array<FloatArray>): Float? {
        if (window.size != windowSamples) return null
        val input = ByteBuffer.allocateDirect(4 * windowSamples * 4).order(ByteOrder.nativeOrder())
        for (row in window) for (v in row) input.putFloat(v)
        input.rewind()
        val output = Array(1) { FloatArray(1) }
        return runCatching { interpreter.run(input, output); output[0][0] }.getOrNull()
    }

    companion object {
        const val MODEL_ASSET = "fall_model.tflite"
        const val SPEC_ASSET = "fall_model.json"

        /** The model and its spec from the assets, or null when the watch has none. */
        fun load(context: Context): Pair<Inference, FallModelSpec>? = runCatching {
            val spec = context.assets.open(SPEC_ASSET).use { FallModelSpec.parse(it) }
            val fd = context.assets.openFd(MODEL_ASSET)
            val buffer = FileInputStream(fd.fileDescriptor).channel.map(FileChannel.MapMode.READ_ONLY, fd.startOffset, fd.declaredLength)
            val digest = MessageDigest.getInstance("SHA-256").apply { update(buffer.duplicate()) }.digest()
            if (!spec.mayRun(digest.joinToString("") { "%02x".format(it) })) return@runCatching null
            val options = Interpreter.Options().setNumThreads(1)
            TfLiteInference(Interpreter(buffer, options), spec.windowSamples) to spec
        }.getOrNull()
    }
}
