import com.fasterxml.jackson.core.type.TypeReference
import com.fasterxml.jackson.databind.ObjectMapper
import io.pebbletemplates.pebble.PebbleEngine
import io.pebbletemplates.pebble.loader.FileLoader
import java.io.File
import java.io.StringWriter

/**
 * Renders every *.json file under an input directory through a single Pebble template,
 * writing each result to the same relative path under an output directory with a .html
 * extension instead of .json - i.e. the output tree mirrors the input tree.
 *
 * Usage: RenderHtml <inputDir> <outputDir> [templatePath]
 *   inputDir     directory to recursively search for *.json files
 *   outputDir    directory to write the mirrored *.html tree into
 *   templatePath path to the Pebble template (default: ../peb.peb.txt, i.e.
 *                JavaTemplated/peb.peb.txt - this project's sibling in the repo)
 */
fun main(args: Array<String>) {
    if (args.size < 2) {
        System.err.println("Usage: RenderHtml <inputDir> <outputDir> [templatePath]")
        kotlin.system.exitProcess(1)
    }

    val inputDir = File(args[0]).absoluteFile
    val outputDir = File(args[1]).absoluteFile
    // Relative to the current working directory, which is this project's directory when
    // invoked the documented way (`cd pebble-renderer && ./gradlew run ...`) - making
    // JavaTemplated/peb.peb.txt, this project's sibling in the repo, the default.
    val templateFile = File(args.getOrElse(2) { "../peb.peb.txt" }).absoluteFile

    require(inputDir.isDirectory) { "Input directory not found: $inputDir" }
    require(templateFile.isFile) { "Template file not found: $templateFile" }

    val loader = FileLoader(templateFile.parentFile.absolutePath)
    val engine = PebbleEngine.Builder().loader(loader).build()
    val template = engine.getTemplate(templateFile.name)

    val mapper = ObjectMapper()
    val mapType = object : TypeReference<Map<String, Any?>>() {}

    var rendered = 0
    var failed = 0
    val failures = mutableListOf<Pair<File, Exception>>()

    inputDir.walkTopDown()
        .filter { it.isFile && it.extension == "json" }
        .forEach { jsonFile ->
            val relative = jsonFile.relativeTo(inputDir).path
            val outFile = File(outputDir, relative.removeSuffix(".json") + ".html")

            try {
                val context: Map<String, Any?> = mapper.readValue(jsonFile, mapType)

                outFile.parentFile.mkdirs()
                outFile.bufferedWriter().use { writer ->
                    template.evaluate(writer, context)
                }
                rendered++
            } catch (e: Exception) {
                failed++
                failures.add(jsonFile to e)
            }

            val total = rendered + failed
            if (total % 1000 == 0) {
                println("...$total files processed ($rendered rendered, $failed failed)")
            }
        }

    println()
    println("Done. Rendered $rendered file(s) to $outputDir")
    if (failed > 0) {
        println("Failed to render $failed file(s):")
        failures.take(20).forEach { (file, e) ->
            println("  - ${file.relativeTo(inputDir)}: ${e::class.simpleName}: ${e.message}")
        }
        if (failures.size > 20) {
            println("  ... and ${failures.size - 20} more")
        }
    }
}
