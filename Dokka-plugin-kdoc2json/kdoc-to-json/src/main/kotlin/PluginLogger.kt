package my.dokka.plugin

import org.jetbrains.dokka.utilities.DokkaLogger
import java.io.File

class PluginLogger(
    private val dokkaLogger: DokkaLogger, 
    levelStr: String,
    logFilePath: String? = null // <--- ADD THIS
) {
    private val level = when (levelStr.lowercase()) {
        "debug" -> 0
        "info" -> 1
        "warn" -> 2
        "error" -> 3
        else -> 1
    }

    private val logFile: File? = logFilePath?.let { path ->
        val file = File(path)
        file.parentFile?.mkdirs()
        file
    }

    // Synchronize to prevent mangled logs if Dokka processes multiple modules in parallel
    private fun writeToFile(msg: String) {
        if (logFile != null) {
            synchronized(this) {
                logFile.appendText("$msg\n")
            }
        }
    }

    fun debug(msg: String) { 
        if (level <= 0) {
            val formatted = "[JSON Plugin DEBUG] $msg"
            dokkaLogger.info(formatted)
            writeToFile(formatted)
        }
    }
    
    fun info(msg: String) { 
        if (level <= 1) {
            val formatted = "[JSON Plugin INFO] $msg"
            dokkaLogger.info(formatted)
            writeToFile(formatted)
        }
    }
    
    fun warn(msg: String) { 
        if (level <= 2) {
            val formatted = "[JSON Plugin WARN] $msg"
            dokkaLogger.warn(formatted)
            writeToFile(formatted)
        }
    }
    
    fun error(msg: String) { 
        if (level <= 3) {
            val formatted = "[JSON Plugin ERROR] $msg"
            dokkaLogger.error(formatted)
            writeToFile(formatted)
        }
    }
}