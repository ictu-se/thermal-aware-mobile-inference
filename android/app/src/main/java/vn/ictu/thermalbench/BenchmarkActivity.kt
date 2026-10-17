package vn.ictu.thermalbench

import android.app.Activity
import android.content.Intent
import android.content.IntentFilter
import android.os.*
import android.view.WindowManager
import android.widget.TextView
import com.google.ai.edge.litertlm.*
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import org.json.JSONObject

@OptIn(ExperimentalApi::class)
class BenchmarkActivity : Activity() {
  private val running = AtomicBoolean(false)
  private lateinit var status: TextView
  private lateinit var directory: File
  private lateinit var power: PowerManager
  private var monitoring = "full"
  private var lastHeadroomNs = Long.MIN_VALUE
  private var sessionOutcome = "completed"
  private var measuredRequests = 0
  private var firstRequestNs = 0L
  private var lastRequestEndNs = 0L
  private var wake: PowerManager.WakeLock? = null

  private fun now() = SystemClock.elapsedRealtimeNanos()

  private fun stamp(obj: JSONObject) =
      obj.put("device_monotonic_ns", now()).put("device_wall_ms", System.currentTimeMillis())

  @Synchronized
  private fun append(file: String, obj: JSONObject) {
    File(directory, file).appendText(stamp(obj).toString() + "\n")
  }

  private fun note(text: String) {
    runOnUiThread { status.text = text }
  }

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    setShowWhenLocked(true)
    setTurnScreenOn(true)
    window.attributes = window.attributes.apply { screenBrightness = 0.05f }
    status =
        TextView(this).apply {
          textSize = 18f
          setPadding(24, 24, 24, 24)
          text = "Thermal Inference Lab"
        }
    setContentView(status)
    window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    power = getSystemService(POWER_SERVICE) as PowerManager
    val id = intent.getStringExtra("run_id") ?: "manual-${System.currentTimeMillis()}"
    require(id.matches(Regex("[A-Za-z0-9_-]+")))
    directory = File(getExternalFilesDir(null), "runs/$id").apply { mkdirs() }
    if (File(directory, "done.json").exists()) {
      note("Run already complete: $id")
      return
    }
    wake =
        power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "thermalbench:run").apply {
          acquire(90 * 60 * 1000L)
        }
    monitoring = intent.getStringExtra("monitoring") ?: "full"
    require(monitoring == "full" || monitoring == "minimal")
    running.set(true)
    val listener = PowerManager.OnThermalStatusChangedListener { value ->
      append("thermal_events.jsonl", JSONObject().put("severity", value))
    }
    power.addThermalStatusListener(listener)
    val telemetryThread =
        Thread {
              while (running.get()) {
                try {
                  if (monitoring == "full") sampleTelemetry()
                } catch (e: Exception) {
                  append("telemetry_errors.jsonl", JSONObject().put("error", e.toString()))
                }
                Thread.sleep(1000)
              }
            }
            .apply { start() }
    Thread {
          try {
            execute(id)
          } catch (e: Throwable) {
            sessionOutcome = "execution_error"
            append(
                "results.jsonl",
                JSONObject().put("status", "error").put("error", e.stackTraceToString()),
            )
            note("Failed: ${e.message}")
          } finally {
            running.set(false)
            telemetryThread.join(3000)
            power.removeThermalStatusListener(listener)
            File(directory, "done.json")
                .writeText(stamp(JSONObject().put("complete", true).put("outcome", sessionOutcome)
                    .put("measured_requests", measuredRequests).put("first_request_ns", firstRequestNs)
                    .put("last_request_end_ns", lastRequestEndNs)).toString())
            wake?.let { if (it.isHeld) it.release() }
            runOnUiThread { window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON) }
          }
        }
        .start()
  }

  private fun sampleTelemetry() {
    val sampledAt = now()
    if (lastHeadroomNs == Long.MIN_VALUE || sampledAt - lastHeadroomNs >= 10_000_000_000L) {
      lastHeadroomNs = sampledAt
      val value = power.getThermalHeadroom(0)
      append("thermal_headroom.jsonl", JSONObject()
          .put("forecast_seconds", 0).put("available", value.isFinite())
          .put("headroom", if (value.isFinite()) value.toDouble() else JSONObject.NULL))
    }
    val battery = registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
    val mem = Debug.MemoryInfo()
    Debug.getMemoryInfo(mem)
    append(
        "device_telemetry.jsonl",
        JSONObject()
            .put("interactive", power.isInteractive)
            .put("thermal_status", power.currentThermalStatus)
            .put("battery_level", battery?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1))
            .put("battery_plugged", battery?.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1))
            .put("battery_status", battery?.getIntExtra(BatteryManager.EXTRA_STATUS, -1))
            .put(
                "battery_temperature_deci_c",
                battery?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1),
            )
            .put("battery_voltage_mv", battery?.getIntExtra(BatteryManager.EXTRA_VOLTAGE, -1))
            .put("total_pss_kb", mem.totalPss)
            .put("native_pss_kb", mem.nativePss)
            .put("process_cpu_ms", android.os.Process.getElapsedCpuTime()),
    )
  }

  private fun metrics(info: BenchmarkInfo) =
      JSONObject()
          .put("native_init_s", info.initTimeInSecond)
          .put("native_ttft_s", info.timeToFirstTokenInSecond)
          .put(
              "native_ttft_definition",
              "SDK proxy: prefill duration plus mean decode duration per token; not observed first token",
          )
          .put("actual_prefill_tokens", info.lastPrefillTokenCount)
          .put("actual_decode_tokens", info.lastDecodeTokenCount)
          .put("native_prefill_tokens_per_s", info.lastPrefillTokensPerSecond)
          .put("native_decode_tokens_per_s", info.lastDecodeTokensPerSecond)

  private fun saveLibraries() {
    try {
      File(directory, "loaded_libraries.txt")
          .writeText(
              File("/proc/self/maps")
                  .readLines()
                  .mapNotNull { it.substringAfterLast(' ').takeIf { path -> path.endsWith(".so") } }
                  .distinct()
                  .sorted()
                  .joinToString("\n")
          )
    } catch (e: Exception) {
      File(directory, "loaded_libraries_error.txt").writeText(e.toString())
    }
  }

  private fun execute(id: String) {
    val mode = intent.getStringExtra("mode") ?: "fixed"
    val model = intent.getStringExtra("model_path") ?: ""
    val backendName = intent.getStringExtra("backend") ?: "NPU"
    val prefill = intent.getIntExtra("prefill_tokens", 128)
    val decode = intent.getIntExtra("decode_tokens", 64)
    val durationSeconds = intent.getIntExtra("duration_seconds", 0)
    var runDeadline = Long.MAX_VALUE
    val iterations = intent.getIntExtra("iterations", 1)
    val warmup = intent.getIntExtra("warmup", 0)
    val prompt =
        intent.getStringExtra("prompt")
            ?: "Explain edge AI on a mobile phone in two short sentences."
    require(iterations in 1..1000 && warmup in 0..10 && prefill in 1..4096 && decode in 1..1024)
    val meta =
        stamp(
            JSONObject()
                .put("run_id", id)
                .put("mode", mode)
                .put("model_path", model)
                .put("backend_requested", backendName)
                .put("backend_independently_verified", false)
                .put("prefill_tokens_requested", prefill)
                .put("decode_tokens_requested", decode)
                .put("duration_seconds", durationSeconds)
                .put("iterations", iterations)
                .put("warmup", warmup)
                .put("prompt", prompt)
                .put("image_supplied", false)
                .put("runtime", "LiteRT-LM")
                .put("runtime_version", "0.18.0")
                .put("application_version", "0.2.0")
                .put("monitoring", monitoring)
                .put("headroom_period_seconds", if (monitoring == "full") 10 else JSONObject.NULL)
                .put("fixed_timer_boundary", "benchmark invocation only; event/log writes outside timer; engine recreation inside benchmark")
                .put("duration_origin", "first measured benchmark invocation")
                .put("firmware", Build.FINGERPRINT)
                .put(
                    "display_policy",
                    "activity over keyguard, keep screen on, per-window brightness 0.05; host records system brightness",
                )
                .put("timing_unit", "native metrics seconds; app durations nanoseconds")
                .put(
                    "fixed_engine_policy",
                    "engine recreated by SDK benchmark API per request; cache retained",
                )
                .put(
                    "natural_engine_policy",
                    "one initialized engine, fresh conversation per request",
                )
        )
    File(directory, "metadata.json").writeText(meta.toString(2))
    if (mode == "selftest") {
      val start = now()
      Thread.sleep(100)
      append(
          "results.jsonl",
          JSONObject()
              .put("status", "ok")
              .put("mode", mode)
              .put("requested_sleep_ms", 100)
              .put("observed_elapsed_ns", now() - start),
      )
      note("Clock and telemetry self-test complete")
      return
    }
    if (mode == "idle") {
      Thread.sleep(durationSeconds.coerceIn(1, 1800) * 1000L)
      append("results.jsonl", JSONObject().put("status", "ok").put("mode", "idle"))
      return
    }
    require(File(model).isFile) { "Model does not exist: $model" }
    val backend =
        when (backendName) {
          "CPU" -> Backend.CPU()
          "GPU" -> Backend.GPU()
          "NPU" -> Backend.NPU(applicationInfo.nativeLibraryDir)
          else -> error("Unknown backend")
        }
    android.system.Os.setenv(
        "ADSP_LIBRARY_PATH",
        applicationInfo.nativeLibraryDir + ";/vendor/lib/rfsa/adsp;/vendor/dsp;/dsp",
        true,
    )
    ExperimentalFlags.enableBenchmark = true
    ExperimentalFlags.enableSpeculativeDecoding = false
    Engine.setNativeMinLogSeverity(LogSeverity.INFO)
    val cache = File(cacheDir, "${File(model).name}-$backendName").apply { mkdirs() }
    if (mode == "fixed") {
      for (i in 0 until iterations + warmup) {
        if (i >= warmup && now() >= runDeadline) {
          sessionOutcome = "duration_complete"
          break
        }
        if (power.currentThermalStatus >= PowerManager.THERMAL_STATUS_SEVERE) {
          sessionOutcome = "thermal_stop"
          break
        }
        append(
            "events.jsonl",
            JSONObject()
                .put("event", "request_start")
                .put("iteration", i)
                .put("phase", if (i < warmup) "warmup" else "measure"),
        )
        Trace.beginSection("fixed_inference_$backendName")
        val start = now()
        if (i == warmup) {
          firstRequestNs = start
          if (durationSeconds > 0) runDeadline = start + durationSeconds * 1_000_000_000L
        }
        val info =
            benchmark(
                modelPath = model,
                backend = backend,
                prefillTokens = prefill,
                decodeTokens = decode,
                cacheDir = cache.path,
                prompt = prompt,
            )
        val end = now()
        lastRequestEndNs = end
        if (i >= warmup) measuredRequests++
        Trace.endSection()
        append(
            "results.jsonl",
            metrics(info)
                .put("iteration", i)
                .put("phase", if (i < warmup) "warmup" else "measure")
                .put(
                    "status",
                    if (
                        info.lastPrefillTokenCount == prefill && info.lastDecodeTokenCount == decode
                    )
                        "ok"
                    else "token_budget_mismatch",
                )
                .put("app_call_elapsed_ns", end - start).put("app_end_ns", end)
                .put("app_start_ns", start)
                .put("backend_requested", backendName),
        )
        if (i == 0) saveLibraries()
        note("$id: fixed ${i+1}/${iterations+warmup}")
      }
      if (durationSeconds > 0 && sessionOutcome == "completed") {
        sessionOutcome = if (now() >= runDeadline) "duration_complete" else "iteration_limit"
      }
    } else if (mode == "natural") {
      val loadStart = now()
      Engine(EngineConfig(modelPath = model, backend = backend, cacheDir = cache.path)).use { engine
        ->
        Trace.beginSection("engine_initialize")
        engine.initialize()
        val loadEnd = now()
        Trace.endSection()
        val loadNs = loadEnd - loadStart
        append("engine_events.jsonl", JSONObject().put("start_ns", loadStart).put("end_ns", loadEnd).put("elapsed_ns", loadNs))
        for (i in 0 until iterations + warmup) {
          if (i > 0 && now() >= runDeadline) break
          check(power.currentThermalStatus < PowerManager.THERMAL_STATUS_SEVERE) {
            "Stopped at severe thermal status"
          }
          engine
              .createConversation(
                  ConversationConfig(
                      samplerConfig =
                          SamplerConfig(topK = 1, topP = 1.0, temperature = 0.0, seed = 42),
                      maxOutputToken = decode,
                  )
              )
              .use { conversation ->
                val done = CountDownLatch(1)
                val first = AtomicLong(0L)
                val callbackEnd = AtomicLong(0L)
                val failure = AtomicReference<Throwable?>(null)
                val response = StringBuilder()
                val start = now()
                if (i == warmup) firstRequestNs = start
                conversation.sendMessageAsync(
                    prompt,
                    object : MessageCallback {
                      override fun onMessage(message: com.google.ai.edge.litertlm.Message) {
                        val received = now()
                        val text = message.toString()
                        append(
                            "stream_chunks.jsonl",
                            JSONObject()
                                .put("iteration", i)
                                .put("received_ns", received)
                                .put("text", text)
                                .put("channels", JSONObject(message.channels)),
                        )
                        if (text.isNotEmpty()) first.compareAndSet(0L, received)
                        synchronized(response) { response.append(text) }
                      }

                      override fun onDone() {
                        callbackEnd.set(now())
                        done.countDown()
                      }

                      override fun onError(throwable: Throwable) {
                        failure.set(throwable)
                        callbackEnd.set(now())
                        done.countDown()
                      }
                    },
                )
                check(done.await(180, TimeUnit.SECONDS)) { "Request callback timed out" }
                failure.get()?.let { throw it }
                val end = callbackEnd.get()
                lastRequestEndNs = end
                if (i >= warmup) measuredRequests++
                val info = conversation.getBenchmarkInfo()
                append(
                    "results.jsonl",
                    metrics(info)
                        .put("phase", if (i < warmup) "warmup" else "measure")
                        .put("iteration", i)
                        .put("status", if (info.lastDecodeTokenCount > 0) "ok" else "zero_token")
                        .put(
                            "observed_first_nonempty_chunk_ns",
                            if (first.get() > 0) first.get() - start else JSONObject.NULL,
                        )
                        .put(
                            "first_chunk_definition",
                            "first nonempty text callback, includes streaming and SDK scheduling; may contain multiple tokens",
                        )
                        .put("engine_load_ns", loadNs)
                        .put("app_generation_elapsed_ns", end - start)
                        .put("app_start_ns", start)
                        .put(
                            "stop_reason",
                            "SDK did not expose exact stop reason; EOS or output cap",
                        )
                        .put("response_text", response.toString())
                        .put("backend_requested", backendName),
                )
              }
          saveLibraries()
          note("$id: natural ${i+1}/${iterations+warmup}")
        }
      }
    } else error("Unsupported mode")
    note("Run complete: $id")
  }
}
