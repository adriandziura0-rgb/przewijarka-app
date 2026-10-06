package pl.przewijak.live

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import com.chaquo.python.Python
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

class MainActivity : Activity() {
    private lateinit var root: FrameLayout
    private lateinit var sourceLayer: FrameLayout
    private lateinit var dashboard: WebView
    private lateinit var sourceToolbar: LinearLayout
    private lateinit var sourceTitle: TextView
    private lateinit var core: PhoneCore
    private lateinit var collectors: CollectorManager
    private lateinit var storage: StorageAccess
    private val prefs by lazy { getSharedPreferences("przewijak_phone", MODE_PRIVATE) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setKeepScreenOn(prefs.getBoolean("screen_awake", false))

        storage = StorageAccess(this, prefs)
        core = PhoneCore(this)
        core.init(
            prefs.getInt("snapshot_interval", 5),
            prefs.getBoolean("log_odds_changes", false)
        )

        root = FrameLayout(this)
        sourceLayer = FrameLayout(this)
        dashboard = buildDashboardWebView()
        collectors = CollectorManager(this, sourceLayer, core, ::showSource)
        collectors.applyProfiles(loadProfiles())

        root.addView(sourceLayer, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT
        ))
        buildSourceToolbar()
        root.addView(dashboard, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT
        ))
        setContentView(root)
        dashboard.loadUrl("file:///android_asset/web/index.html")
    }

    private fun buildDashboardWebView(): WebView = WebView(this).apply {
        settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = true
            allowContentAccess = false
            databaseEnabled = false
            setSupportZoom(false)
        }
        webChromeClient = WebChromeClient()
        webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val uri = request?.url ?: return false
                if (uri.scheme == "file") return false
                return try {
                    startActivity(Intent(Intent.ACTION_VIEW, uri))
                    true
                } catch (_: Exception) { true }
            }
        }
        addJavascriptInterface(DashboardBridge(), "PrzewijakAndroid")
    }

    private fun buildSourceToolbar() {
        sourceToolbar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(Color.rgb(11, 16, 32))
            setPadding(12.dp(), 8.dp(), 12.dp(), 8.dp())
        }
        val back = Button(this).apply {
            text = "← PANEL"
            setOnClickListener { showDashboard() }
        }
        sourceTitle = TextView(this).apply {
            text = "ŹRÓDŁO"
            setTextColor(Color.WHITE)
            textSize = 14f
            setPadding(12.dp(), 0, 0, 0)
        }
        sourceToolbar.addView(back, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, 48.dp()))
        sourceToolbar.addView(sourceTitle, LinearLayout.LayoutParams(0, 48.dp(), 1f))
        val lp = FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 64.dp()).apply {
            gravity = Gravity.TOP
        }
        sourceLayer.addView(sourceToolbar, lp)
    }

    private fun showSource(id: String, label: String) {
        runOnUiThread {
            collectors.bringSourceToFront(id)
            sourceTitle.text = label
            sourceToolbar.bringToFront()
            dashboard.visibility = View.GONE
        }
    }

    private fun showDashboard() {
        runOnUiThread {
            dashboard.visibility = View.VISIBLE
            dashboard.bringToFront()
            dashboard.evaluateJavascript("try{refresh()}catch(e){}", null)
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (dashboard.visibility != View.VISIBLE) showDashboard() else super.onBackPressed()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        val result = storage.handleActivityResult(requestCode, resultCode, data) ?: return
        runOnUiThread {
            val quoted = JSONObject.quote(result.toString())
            dashboard.evaluateJavascript(
                "try{window.PrzewijakStorageEvent && window.PrzewijakStorageEvent(JSON.parse($quoted));refresh()}catch(e){}",
                null
            )
        }
    }

    override fun onDestroy() {
        try { collectors.destroy() } catch (_: Exception) {}
        try { dashboard.removeJavascriptInterface("PrzewijakAndroid") } catch (_: Exception) {}
        try { dashboard.destroy() } catch (_: Exception) {}
        super.onDestroy()
    }

    private fun setKeepScreenOn(enabled: Boolean) {
        runOnUiThread {
            if (enabled) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    private fun openAppSettings() {
        runOnUiThread {
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                data = Uri.parse("package:$packageName")
            })
        }
    }

    private fun exportToSelectedFolder(): JSONObject {
        val prepared = JSONObject(core.prepareExport(cacheDir.absolutePath))
        if (!prepared.optBoolean("ok", false)) {
            return JSONObject().put("ok", false).put("error", prepared.optString("error", "Nie udało się przygotować eksportu."))
        }
        val dir = File(prepared.optString("snapshot_dir", ""))
        return storage.exportDirectory(dir)
    }

    private fun loadProfiles(): List<SourceProfile> {
        val raw = prefs.getString("source_profiles_json", null)
        if (!raw.isNullOrBlank()) {
            try { return SourceProfile.fromArray(JSONArray(raw)) } catch (_: Exception) {}
        }
        return listOf(
            SourceProfile("fortuna", "FORTUNA", "fortuna", "", false),
            SourceProfile("superbet", "SUPERBET", "superbet", "", false),
            SourceProfile("betcris", "BETCRIS / BETSPORT", "betcris_pl", "", false)
        )
    }

    private fun saveProfiles(profiles: List<SourceProfile>) {
        val arr = JSONArray()
        profiles.forEach { arr.put(it.toJson()) }
        prefs.edit().putString("source_profiles_json", arr.toString()).apply()
    }

    inner class DashboardBridge {
        @JavascriptInterface
        fun request(method: String, path: String, body: String?): String {
            return try {
                val payload = if (body.isNullOrBlank()) JSONObject() else JSONObject(body)
                when {
                    method.equals("GET", true) && path == "/api/status" -> statusJson().toString()
                    method.equals("GET", true) && path == "/api/source-profiles" -> JSONObject()
                        .put("ok", true).put("profiles", profilesJson()).toString()
                    method.equals("POST", true) && path == "/api/source-profiles" -> {
                        val profiles = SourceProfile.fromArray(payload.optJSONArray("profiles") ?: JSONArray())
                        saveProfiles(profiles)
                        collectors.applyProfiles(profiles)
                        JSONObject().put("ok", true).put("profiles", profilesJson()).toString()
                    }
                    method.equals("POST", true) && path == "/api/live/start" -> core.start()
                    method.equals("POST", true) && path == "/api/live/stop" -> {
                        val stopped = JSONObject(core.stop())
                        if (storage.autoExportEnabled() && storage.statusJson().optBoolean("configured", false)) {
                            stopped.put("export", exportToSelectedFolder())
                        }
                        stopped.toString()
                    }
                    method.equals("POST", true) && path == "/api/browser/start" -> {
                        val started = collectors.startAll()
                        JSONObject().put("ok", true).put("opened", JSONArray(started)).toString()
                    }
                    method.equals("POST", true) && path == "/api/browser/stop" -> {
                        collectors.stopAll()
                        JSONObject().put("ok", true).put("message", "Collector Android zatrzymany.").toString()
                    }
                    method.equals("POST", true) && path == "/api/browser/reinject" -> {
                        val n = collectors.reinjectAll()
                        JSONObject().put("ok", true).put("count", n).toString()
                    }
                    method.equals("POST", true) && path == "/api/browser/open" -> {
                        val id = payload.optString("id")
                        val ok = collectors.openForUser(id)
                        JSONObject().put("ok", ok).put("id", id).toString()
                    }
                    method.equals("GET", true) && path == "/api/storage/status" -> JSONObject()
                        .put("ok", true).put("storage", storage.statusJson()).toString()
                    method.equals("POST", true) && path == "/api/storage/select-folder" -> {
                        runOnUiThread { storage.openFolderPicker() }
                        JSONObject().put("ok", true).put("picker_opened", true).toString()
                    }
                    method.equals("POST", true) && path == "/api/storage/export" -> exportToSelectedFolder().toString()
                    method.equals("POST", true) && path == "/api/storage/clear" -> storage.clearSelection().toString()
                    method.equals("POST", true) && path == "/api/storage/config" -> {
                        val enabled = payload.optBoolean("auto_export_on_stop", true)
                        storage.setAutoExport(enabled)
                        JSONObject().put("ok", true).put("storage", storage.statusJson()).toString()
                    }
                    method.equals("POST", true) && path == "/api/screen-awake" -> {
                        val enabled = payload.optBoolean("enabled", false)
                        prefs.edit().putBoolean("screen_awake", enabled).apply()
                        setKeepScreenOn(enabled)
                        JSONObject().put("ok", true).put("screen_awake", enabled).toString()
                    }
                    method.equals("POST", true) && path == "/api/config" -> {
                        val interval = payload.optInt("snapshot_interval", 5).coerceIn(1, 20)
                        val logOdds = payload.optBoolean("log_odds_changes", false)
                        prefs.edit().putInt("snapshot_interval", interval)
                            .putBoolean("log_odds_changes", logOdds).apply()
                        core.configure(interval, logOdds)
                    }
                    method.equals("POST", true) && path == "/api/open-developer-settings" -> {
                        openAppSettings()
                        JSONObject().put("ok", true).put("message", "Otwarto ustawienia aplikacji Android.").toString()
                    }
                    method.equals("GET", true) && path.startsWith("/api/intelligence/") -> JSONObject()
                        .put("ok", true).put("stage", "PHONE_STAGE3")
                        .put("message", "Warstwa Inteligentna pozostaje odseparowana. Etap 3 dodaje bezpieczny eksport przez Android Storage Access Framework.")
                        .toString()
                    else -> JSONObject().put("ok", false)
                        .put("error", "Nieznana funkcja: $path").toString()
                }
            } catch (e: Exception) {
                JSONObject().put("ok", false)
                    .put("error", "Błąd Android: ${e.message ?: e.javaClass.simpleName}").toString()
            }
        }

        @JavascriptInterface
        fun platform(): String = "ANDROID_PHONE_STAGE3"
    }

    private fun profilesJson(): JSONArray {
        val arr = JSONArray()
        collectors.profiles().forEach { arr.put(it.toJson()) }
        return arr
    }

    private fun statusJson(): JSONObject {
        val liveEnvelope = JSONObject(core.status())
        val live = liveEnvelope.optJSONObject("live") ?: JSONObject()
        val counters = live.optJSONObject("counters") ?: JSONObject()
        val storageStatus = storage.statusJson()
        return JSONObject().put("ok", true)
            .put("platform", "ANDROID")
            .put("version", "PHONE-3.0.0-stage3")
            .put("core_version", "2.9.16 FROZEN")
            .put("core_connected", true)
            .put("screen_awake", prefs.getBoolean("screen_awake", false))
            .put("output_dir", if (storageStatus.optBoolean("configured", false)) storageStatus.optString("display_path") else "Pamięć prywatna aplikacji — brak folderu eksportu")
            .put("storage", storageStatus)
            .put("config", JSONObject()
                .put("snapshot_interval", prefs.getInt("snapshot_interval", 5))
                .put("log_odds_changes", prefs.getBoolean("log_odds_changes", false)))
            .put("source_profiles", profilesJson())
            .put("browser", collectors.statusJson())
            .put("live", JSONObject()
                .put("running", live.optBoolean("running", false))
                .put("session", live.optString("session", ""))
                .put("matches", live.optJSONArray("matches") ?: JSONArray())
                .put("sources", live.optJSONArray("sources") ?: JSONArray())
                .put("recent_events", live.optJSONArray("recent_events") ?: JSONArray())
                .put("goals", counters.optInt("goals", 0))
                .put("changes", counters.optInt("changes", 0))
                .put("anomalies", counters.optInt("anomaly_occurrences", 0))
                .put("raw_packets", counters.optInt("raw_packets", 0))
                .put("accepted_packets", counters.optInt("accepted_packets", 0)))
    }

    private fun Int.dp(): Int = (this * resources.displayMetrics.density).toInt()
}


data class SourceProfile(
    val id: String,
    val label: String,
    val parser: String,
    val url: String,
    val enabled: Boolean
) {
    fun toJson(): JSONObject = JSONObject()
        .put("id", id).put("label", label).put("parser", parser)
        .put("url", url).put("enabled", enabled)
        .put("collect", JSONObject().put("score", true).put("clock", true).put("market", true))

    companion object {
        fun fromArray(arr: JSONArray): List<SourceProfile> {
            val out = mutableListOf<SourceProfile>()
            val seen = mutableSetOf<String>()
            for (i in 0 until minOf(arr.length(), 12)) {
                val o = arr.optJSONObject(i) ?: continue
                val rawId = o.optString("id", "source${i + 1}").lowercase(Locale.ROOT)
                var id = rawId.replace(Regex("[^a-z0-9_-]+"), "_").take(32).ifBlank { "source${i + 1}" }
                if (!seen.add(id)) continue
                val parser = o.optString("parser", "auto").lowercase(Locale.ROOT).let {
                    if (it in setOf("auto", "fortuna", "superbet", "betcris", "betcris_pl")) it else "auto"
                }
                val url = o.optString("url", "").trim()
                val enabled = o.optBoolean("enabled", false) && (url.startsWith("https://") || url.startsWith("http://"))
                out += SourceProfile(
                    id = id,
                    label = o.optString("label", id.uppercase(Locale.ROOT)).take(80),
                    parser = parser,
                    url = url,
                    enabled = enabled
                )
            }
            return out
        }
    }
}


class PhoneCore(private val activity: Activity) {
    private val module by lazy { Python.getInstance().getModule("phone_core_bridge") }

    fun init(interval: Int, logOdds: Boolean): String =
        module.callAttr("init", activity.filesDir.absolutePath, interval, logOdds).toString()

    fun start(): String = module.callAttr("start").toString()
    fun stop(): String = module.callAttr("stop").toString()
    fun status(): String = module.callAttr("status").toString()
    fun configure(interval: Int, logOdds: Boolean): String =
        module.callAttr("configure", interval, logOdds).toString()
    fun heartbeat(payload: String): String = module.callAttr("heartbeat", payload).toString()
    fun ingest(payload: String): String = module.callAttr("ingest", payload).toString()
    fun prepareExport(cacheDir: String): String = module.callAttr("prepare_export", cacheDir).toString()
}


class CollectorManager(
    private val activity: Activity,
    private val container: FrameLayout,
    private val core: PhoneCore,
    private val showSource: (String, String) -> Unit
) {
    private val entries = LinkedHashMap<String, Entry>()
    private var running = false
    private val collectorBody: String by lazy {
        activity.assets.open("web/collector.js").bufferedReader().use { it.readText() }
            .replace("__ENDPOINT__", "/api/ingest")
            .replace("__HEARTBEAT_ENDPOINT__", "/api/heartbeat")
            .replace("__STATUS_ENDPOINT__", "/api/status")
    }

    data class Entry(
        var profile: SourceProfile,
        val webView: WebView,
        var loadedUrl: String = "",
        var injectedAt: Long = 0L,
        var lastPageAt: Long = 0L,
        var lastBridgeAt: Long = 0L,
        var lastError: String = ""
    )

    fun profiles(): List<SourceProfile> = entries.values.map { it.profile }

    fun applyProfiles(profiles: List<SourceProfile>) = onUi {
        val wanted = profiles.associateBy { it.id }
        val remove = entries.keys.filter { it !in wanted.keys }
        remove.forEach { id ->
            val e = entries.remove(id) ?: return@forEach
            try { e.webView.evaluateJavascript("try{window.__PRZEWIJAK_COLLECTOR__?.stop?.()}catch(e){}", null) } catch (_: Exception) {}
            try { container.removeView(e.webView); e.webView.destroy() } catch (_: Exception) {}
        }
        profiles.forEach { p ->
            val existing = entries[p.id]
            if (existing == null) {
                entries[p.id] = Entry(p, makeWebView(p))
            } else {
                val changed = existing.profile.url != p.url || existing.profile.parser != p.parser || existing.profile.enabled != p.enabled
                existing.profile = p
                if (changed) {
                    try { existing.webView.evaluateJavascript("try{window.__PRZEWIJAK_COLLECTOR__?.stop?.()}catch(e){}", null) } catch (_: Exception) {}
                    if (!p.enabled || p.url.isBlank()) {
                        existing.webView.loadUrl("about:blank")
                        existing.loadedUrl = ""
                    } else if (running) {
                        load(existing)
                    }
                }
            }
        }
    }

    private fun makeWebView(profile: SourceProfile): WebView {
        val w = WebView(activity)
        w.setBackgroundColor(Color.BLACK)
        w.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            javaScriptCanOpenWindowsAutomatically = false
            setSupportMultipleWindows(false)
            useWideViewPort = true
            loadWithOverviewMode = true
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            setSupportZoom(true)
            builtInZoomControls = true
            displayZoomControls = false
            val def = WebSettings.getDefaultUserAgent(activity)
            val chrome = Regex("Chrome/[0-9.]+").find(def)?.value ?: "Chrome/131.0.0.0"
            userAgentString = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) $chrome Safari/537.36"
        }
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(w, true)
        w.webChromeClient = WebChromeClient()
        w.addJavascriptInterface(SourceBridge(profile.id), "PrzewijakSourceBridge")
        w.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val uri = request?.url ?: return false
                return if (uri.scheme == "http" || uri.scheme == "https") false else {
                    try { activity.startActivity(Intent(Intent.ACTION_VIEW, uri)) } catch (_: Exception) {}
                    true
                }
            }
            override fun onPageFinished(view: WebView?, url: String?) {
                val e = entries[profile.id] ?: return
                e.loadedUrl = url ?: ""
                e.lastPageAt = System.currentTimeMillis()
                if (running && e.profile.enabled && e.loadedUrl.startsWith("http")) inject(e)
            }
        }
        val lp = FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT).apply {
            topMargin = (64 * activity.resources.displayMetrics.density).toInt()
        }
        container.addView(w, 0, lp)
        return w
    }

    fun startAll(): List<String> = onUi {
        running = true
        val opened = mutableListOf<String>()
        entries.values.filter { it.profile.enabled && it.profile.url.startsWith("http") }.forEach {
            load(it)
            opened += it.profile.id
        }
        opened
    }

    fun stopAll() = onUi {
        running = false
        entries.values.forEach {
            try { it.webView.evaluateJavascript("try{window.__PRZEWIJAK_COLLECTOR__?.stop?.();window.__PRZEWIJAK_COLLECTOR__=null}catch(e){}", null) } catch (_: Exception) {}
        }
    }

    fun reinjectAll(): Int = onUi {
        var count = 0
        entries.values.filter { it.profile.enabled && it.loadedUrl.startsWith("http") }.forEach {
            inject(it)
            count++
        }
        count
    }

    fun openForUser(id: String): Boolean = onUi {
        val e = entries[id] ?: return@onUi false
        if (e.profile.url.startsWith("http") && e.loadedUrl.isBlank()) load(e)
        showSource(e.profile.id, e.profile.label)
        true
    }

    fun bringSourceToFront(id: String) = onUi {
        entries[id]?.webView?.bringToFront()
    }

    private fun load(e: Entry) {
        if (!e.profile.enabled || !e.profile.url.startsWith("http")) return
        if (e.loadedUrl != e.profile.url) {
            e.webView.loadUrl(e.profile.url)
        } else {
            inject(e)
        }
    }

    private fun inject(e: Entry) {
        val profileJson = e.profile.toJson().toString()
        val bootstrap = """
            window.__PRZEWIJAK_SOURCE_CONFIG__ = $profileJson;
            window.__PRZEWIJAK_PLATFORM_OVERRIDE__ = ${JSONObject.quote(e.profile.parser)};
            window.__przewijakNativeSend = function(req) {
              return new Promise(function(resolve, reject) {
                try {
                  var raw = window.PrzewijakSourceBridge.send(JSON.stringify(req || {}));
                  var parsed = JSON.parse(raw || '{}');
                  if (parsed && parsed.__bridge_error) reject(new Error(parsed.__bridge_error));
                  else resolve(parsed);
                } catch (err) { reject(err); }
              });
            };
        """.trimIndent()
        e.webView.evaluateJavascript(bootstrap + "\n" + collectorBody, null)
        e.injectedAt = System.currentTimeMillis()
        e.lastError = ""
    }

    inner class SourceBridge(private val sourceId: String) {
        @JavascriptInterface
        fun send(payloadJson: String): String {
            val e = entries[sourceId] ?: return JSONObject().put("retired", true).put("stop_collector", true).put("ok", true).toString()
            e.lastBridgeAt = System.currentTimeMillis()
            if (!e.profile.enabled) {
                return JSONObject().put("ok", true).put("retired", true).put("stop_collector", true).toString()
            }
            return try {
                val req = JSONObject(payloadJson)
                val url = req.optString("url")
                val data = req.optJSONObject("data") ?: JSONObject()
                when {
                    url.endsWith("/api/heartbeat") || url.endsWith("heartbeat") -> core.heartbeat(data.toString())
                    url.endsWith("/api/ingest") || url.endsWith("ingest") -> core.ingest(data.toString())
                    url.endsWith("/api/status") || url.endsWith("status") -> core.status()
                    else -> JSONObject().put("__bridge_error", "Nieznany kanał collectora: $url").toString()
                }
            } catch (ex: Exception) {
                e.lastError = ex.message ?: ex.javaClass.simpleName
                JSONObject().put("__bridge_error", "Błąd mostu collectora: ${e.lastError}").toString()
            }
        }
    }

    fun statusJson(): JSONObject = onUi {
        val arr = JSONArray()
        entries.values.forEach { e ->
            arr.put(JSONObject()
                .put("id", e.profile.id).put("label", e.profile.label)
                .put("enabled", e.profile.enabled).put("url", e.profile.url)
                .put("loaded_url", e.loadedUrl).put("injected", e.injectedAt > 0)
                .put("last_page_at", e.lastPageAt).put("last_bridge_at", e.lastBridgeAt)
                .put("error", e.lastError))
        }
        JSONObject().put("running", running).put("sources", arr)
    }

    fun destroy() = onUi {
        running = false
        entries.values.forEach {
            try { it.webView.evaluateJavascript("try{window.__PRZEWIJAK_COLLECTOR__?.stop?.()}catch(e){}", null) } catch (_: Exception) {}
            try { it.webView.removeJavascriptInterface("PrzewijakSourceBridge") } catch (_: Exception) {}
            try { it.webView.destroy() } catch (_: Exception) {}
        }
        entries.clear()
    }

    private fun <T> onUi(block: () -> T): T {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
        val latch = CountDownLatch(1)
        val result = AtomicReference<Result<T>>()
        activity.runOnUiThread {
            try { result.set(Result.success(block())) }
            catch (t: Throwable) { result.set(Result.failure(t)) }
            finally { latch.countDown() }
        }
        if (!latch.await(12, TimeUnit.SECONDS)) throw IllegalStateException("Timeout polecenia WebView Android")
        return result.get().getOrThrow()
    }
}
