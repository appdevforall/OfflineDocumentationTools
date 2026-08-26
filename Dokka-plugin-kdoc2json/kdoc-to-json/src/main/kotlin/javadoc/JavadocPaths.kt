package org.appdevforall.dokka.kdoc2json.javadoc

import org.jetbrains.dokka.links.Callable
import org.jetbrains.dokka.links.DRI
import org.jetbrains.dokka.links.JavaClassReference
import org.jetbrains.dokka.links.RecursiveType
import org.jetbrains.dokka.links.StarProjection
import org.jetbrains.dokka.links.TypeConstructor
import org.jetbrains.dokka.links.TypeParam
import org.jetbrains.dokka.links.TypeReference
import org.jetbrains.dokka.links.Vararg

/**
 * Reproduces javadoc's on-disk layout and anchor scheme, with `.json` in place of `.html`.
 *
 * javadoc lays its `api/` tree out as:
 *
 * ```
 * <module>/<package/as/path>/<Outer.Nested>.html   (module dir only for a modular run)
 * <module>/<package/as/path>/package-summary.html
 * <module>/module-summary.html
 * ```
 *
 * and links between those pages relatively (`../lang/Object.html`), which is what makes the tree
 * self-contained when it is served from an arbitrary prefix. [relativeUrl] does the same thing.
 *
 * @param useModuleDirs whether page paths carry a leading `<module>/` segment. Mirrors javadoc's
 *   own split: a modular run gets module directories, a non-modular one puts packages at the root.
 */
class JavadocPaths(private val useModuleDirs: Boolean) {

    companion object {
        const val EXTENSION = "json"
        const val PACKAGE_SUMMARY = "package-summary"
        const val MODULE_SUMMARY = "module-summary"

        /** Dokka models a Java array as a one-argument `kotlin.Array` type constructor. */
        private val ARRAY_FQNS = setOf("kotlin.Array", "java.lang.Array")
    }

    private fun prefix(moduleName: String?): String =
        if (useModuleDirs && !moduleName.isNullOrBlank()) "$moduleName/" else ""

    private fun packageDir(packageName: String?): String =
        if (packageName.isNullOrBlank()) "" else packageName.replace('.', '/') + "/"

    /** `java.base/java/util/Map.Entry.json`. Nested types keep their dotted name, as javadoc does. */
    fun classFile(packageName: String?, classNames: String, moduleName: String?): String =
        "${prefix(moduleName)}${packageDir(packageName)}$classNames.$EXTENSION"

    fun packageFile(packageName: String?, moduleName: String?): String =
        "${prefix(moduleName)}${packageDir(packageName)}$PACKAGE_SUMMARY.$EXTENSION"

    fun moduleFile(moduleName: String): String =
        if (useModuleDirs) "$moduleName/$MODULE_SUMMARY.$EXTENSION" else "$MODULE_SUMMARY.$EXTENSION"

    /**
     * A javadoc-style relative link from the page at [fromFile] to the page at [toFile], both
     * given as output-dir-relative paths. Returns just the file name when they share a directory.
     */
    fun relativeUrl(fromFile: String, toFile: String): String {
        val fromDir = fromFile.split('/').dropLast(1)
        val toParts = toFile.split('/')
        val toDir = toParts.dropLast(1)

        var common = 0
        while (common < fromDir.size && common < toDir.size && fromDir[common] == toDir[common]) {
            common++
        }
        val up = List(fromDir.size - common) { ".." }
        val down = toDir.drop(common) + toParts.last()
        return (up + down).joinToString("/")
    }

    /**
     * javadoc's member anchor: the bare name for a field, and `name(erasedParamTypes)` for an
     * executable -- with constructors spelled `<init>(...)`, as javadoc has done since JDK 18.
     *
     * The parameter types are *erased* and fully qualified, so `<T> T[] toArray(T[] a)` anchors as
     * `toArray(java.lang.Object[])`. That erasure is exactly what Dokka's DRI already carries, so
     * the anchors here line up with the ones in a real javadoc build.
     */
    fun memberAnchor(dri: DRI, isConstructor: Boolean): String {
        val callable = dri.callable ?: return dri.classNames?.substringAfterLast('.') ?: ""
        val name = if (isConstructor) "<init>" else callable.name
        if (isField(callable)) return name
        val params = callable.params.joinToString(",") { erasedTypeName(it) }
        return "$name($params)"
    }

    /**
     * A field's DRI carries no parameter list and is flagged `isProperty`; Dokka also emits
     * zero-arg *methods* though, so `isProperty` is what actually separates the two.
     */
    private fun isField(callable: Callable): Boolean = callable.isProperty

    /** Renders one DRI parameter type the way javadoc spells it inside a member anchor. */
    fun erasedTypeName(ref: TypeReference): String = when (ref) {
        is TypeConstructor -> {
            val fqn = ref.fullyQualifiedName
            if (fqn in ARRAY_FQNS) {
                // A raw `kotlin.Array` with no argument can't be rendered as `X[]`; fall back to
                // Object[] rather than emitting a bare "[]".
                val inner = ref.params.firstOrNull()?.let { erasedTypeName(it) } ?: "java.lang.Object"
                "$inner[]"
            } else {
                fqn
            }
        }
        is JavaClassReference -> ref.name
        // A type variable erases to its leftmost bound, or to Object when unbounded.
        is TypeParam -> ref.bounds.firstOrNull()?.let { erasedTypeName(it) } ?: "java.lang.Object"
        is org.jetbrains.dokka.links.Nullable -> erasedTypeName(ref.wrapped)
        is Vararg -> "${erasedTypeName(ref.elementType)}[]"
        is StarProjection -> "java.lang.Object"
        is RecursiveType -> "java.lang.Object"
        else -> ref.toString()
    }
}
