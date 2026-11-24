package care.ira.watch.sensors

import android.content.Context
import org.tensorflow.lite.Interpreter
import java.io.FileInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.channels.FileChannel

/**
 * The trained fall model through TensorFlow Lite, from the app's assets
 * (`fall_model.tflite`, written by scripts/training/run_kaggle.py fall). Built once;
 * `load` returns null when the asset is missing or the interpreter cannot start,
 * and the caller then runs the rule detector alone.
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
            val options = Interpreter.Options().setNumThreads(1)
            TfLiteInference(Interpreter(buffer, options), spec.windowSamples) to spec
        }.getOrNull()
    }
}
