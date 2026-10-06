package pl.przewijak.live

import android.app.Activity
import android.content.Intent
import android.content.SharedPreferences
import android.net.Uri
import android.provider.DocumentsContract
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.util.Locale

class StorageAccess(
    private val activity: Activity,
    private val prefs: SharedPreferences
) {
    companion object {
        const val REQUEST_TREE = 7301
        private const val PREF_TREE_URI = "export_tree_uri"
        private const val PREF_AUTO_EXPORT = "auto_export_on_stop"
    }

    fun openFolderPicker() {
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).apply {
            addFlags(
                Intent.FLAG_GRANT_READ_URI_PERMISSION or
                    Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                    Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION or
                    Intent.FLAG_GRANT_PREFIX_URI_PERMISSION
            )
        }
        activity.startActivityForResult(intent, REQUEST_TREE)
    }

    fun handleActivityResult(requestCode: Int, resultCode: Int, data: Intent?): JSONObject? {
        if (requestCode != REQUEST_TREE) return null
        if (resultCode != Activity.RESULT_OK) {
            return JSONObject().put("ok", false).put("cancelled", true).put("message", "Nie wybrano folderu.")
        }
        val resultIntent = data ?: return JSONObject().put("ok", false).put("message", "Android nie zwrócił folderu.")
        val uri = resultIntent.data ?: return JSONObject().put("ok", false).put("message", "Android nie zwrócił folderu.")
        val flags = resultIntent.flags and (Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        try {
            activity.contentResolver.takePersistableUriPermission(uri, flags)
        } catch (_: Exception) {
            // Some providers already persist the permission or don't expose the flag cleanly.
        }
        prefs.edit().putString(PREF_TREE_URI, uri.toString()).apply()
        return JSONObject().put("ok", true).put("storage", statusJson())
    }

    fun setAutoExport(enabled: Boolean) {
        prefs.edit().putBoolean(PREF_AUTO_EXPORT, enabled).apply()
    }

    fun autoExportEnabled(): Boolean = prefs.getBoolean(PREF_AUTO_EXPORT, true)

    fun clearSelection(): JSONObject {
        val raw = prefs.getString(PREF_TREE_URI, null)
        if (!raw.isNullOrBlank()) {
            try {
                activity.contentResolver.releasePersistableUriPermission(
                    Uri.parse(raw),
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                )
            } catch (_: Exception) {}
        }
        prefs.edit().remove(PREF_TREE_URI).apply()
        return JSONObject().put("ok", true).put("storage", statusJson())
    }

    fun statusJson(): JSONObject {
        val uri = selectedTreeUri()
        val configured = uri != null
        return JSONObject()
            .put("configured", configured)
            .put("display_path", if (configured) displayPath(uri!!) else "Nie wybrano folderu eksportu")
            .put("is_sd_card", if (configured) isLikelySd(uri!!) else false)
            .put("auto_export_on_stop", autoExportEnabled())
    }

    fun exportDirectory(snapshotRoot: File): JSONObject {
        val tree = selectedTreeUri() ?: return JSONObject()
            .put("ok", false)
            .put("error", "Najpierw wybierz folder eksportu.")
        if (!snapshotRoot.isDirectory) {
            return JSONObject().put("ok", false).put("error", "Brak przygotowanego snapshotu eksportu.")
        }

        return try {
            val resolver = activity.contentResolver
            val rootDocId = DocumentsContract.getTreeDocumentId(tree)
            val rootDocUri = DocumentsContract.buildDocumentUriUsingTree(tree, rootDocId)
            val appDir = ensureDirectory(tree, rootDocUri, "PrzewijakLIVE")
            var files = 0
            var bytes = 0L
            snapshotRoot.listFiles()?.sortedBy { it.name }?.forEach { child ->
                val stats = copyRecursive(tree, appDir, child)
                files += stats.first
                bytes += stats.second
            }
            JSONObject()
                .put("ok", true)
                .put("files", files)
                .put("bytes", bytes)
                .put("display_path", displayPath(tree) + "/PrzewijakLIVE")
                .put("is_sd_card", isLikelySd(tree))
        } catch (e: Exception) {
            JSONObject().put("ok", false).put("error", "Eksport nie powiódł się: ${e.message ?: e.javaClass.simpleName}")
        }
    }

    private fun selectedTreeUri(): Uri? {
        val raw = prefs.getString(PREF_TREE_URI, null) ?: return null
        val uri = try { Uri.parse(raw) } catch (_: Exception) { return null }
        val hasPersisted = activity.contentResolver.persistedUriPermissions.any {
            it.uri == uri && it.isWritePermission
        }
        return if (hasPersisted) uri else null
    }

    private fun displayPath(uri: Uri): String {
        return try {
            val id = DocumentsContract.getTreeDocumentId(uri)
            val parts = id.split(":", limit = 2)
            val volume = parts.getOrNull(0).orEmpty()
            val path = parts.getOrNull(1).orEmpty().trim('/')
            val base = if (volume.equals("primary", true)) "Pamięć wewnętrzna" else "Karta SD ($volume)"
            if (path.isBlank()) base else "$base/$path"
        } catch (_: Exception) {
            "Wybrany folder Android"
        }
    }

    private fun isLikelySd(uri: Uri): Boolean {
        return try {
            val id = DocumentsContract.getTreeDocumentId(uri)
            val volume = id.substringBefore(":")
            volume.isNotBlank() && !volume.equals("primary", true)
        } catch (_: Exception) { false }
    }

    private fun ensureDirectory(tree: Uri, parent: Uri, rawName: String): Uri {
        val name = safeName(rawName)
        findChild(tree, parent, name, DocumentsContract.Document.MIME_TYPE_DIR)?.let { return it }
        return DocumentsContract.createDocument(
            activity.contentResolver,
            parent,
            DocumentsContract.Document.MIME_TYPE_DIR,
            name
        ) ?: throw IllegalStateException("Nie można utworzyć katalogu $name")
    }

    private fun ensureFile(tree: Uri, parent: Uri, rawName: String, mime: String): Uri {
        val name = safeName(rawName)
        val existing = findChild(tree, parent, name, null)
        if (existing != null) return existing
        return DocumentsContract.createDocument(activity.contentResolver, parent, mime, name)
            ?: throw IllegalStateException("Nie można utworzyć pliku $name")
    }

    private fun findChild(tree: Uri, parent: Uri, name: String, requiredMime: String?): Uri? {
        val parentId = DocumentsContract.getDocumentId(parent)
        val children = DocumentsContract.buildChildDocumentsUriUsingTree(tree, parentId)
        val projection = arrayOf(
            DocumentsContract.Document.COLUMN_DOCUMENT_ID,
            DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_MIME_TYPE
        )
        activity.contentResolver.query(children, projection, null, null, null)?.use { cursor ->
            while (cursor.moveToNext()) {
                val childId = cursor.getString(0)
                val childName = cursor.getString(1)
                val childMime = cursor.getString(2)
                if (childName == name && (requiredMime == null || childMime == requiredMime)) {
                    return DocumentsContract.buildDocumentUriUsingTree(tree, childId)
                }
            }
        }
        return null
    }

    private fun copyRecursive(tree: Uri, parent: Uri, source: File): Pair<Int, Long> {
        return if (source.isDirectory) {
            val childDir = ensureDirectory(tree, parent, source.name)
            var files = 0
            var bytes = 0L
            source.listFiles()?.sortedBy { it.name }?.forEach { child ->
                val stats = copyRecursive(tree, childDir, child)
                files += stats.first
                bytes += stats.second
            }
            files to bytes
        } else {
            val mime = mimeFor(source.name)
            val target = ensureFile(tree, parent, source.name, mime)
            activity.contentResolver.openOutputStream(target, "wt")?.use { out ->
                FileInputStream(source).use { input -> input.copyTo(out) }
            } ?: throw IllegalStateException("Brak zapisu do ${source.name}")
            1 to source.length()
        }
    }

    private fun safeName(value: String): String = value
        .replace(Regex("[\\/:*?\"<>|]+"), "_")
        .trim()
        .take(120)
        .ifBlank { "plik" }

    private fun mimeFor(name: String): String = when (name.substringAfterLast('.', "").lowercase(Locale.ROOT)) {
        "json", "jsonl" -> "application/json"
        "csv" -> "text/csv"
        "txt", "log", "md" -> "text/plain"
        "sqlite3", "db" -> "application/vnd.sqlite3"
        else -> "application/octet-stream"
    }
}
