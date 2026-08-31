package org.appdevforall.dokka.kdoc2json.javadoc

import kotlinx.serialization.Serializable

/**
 * DTOs mirroring the pages the `javadoc` tool emits under its `api/` output directory.
 *
 * These are deliberately *not* part of the `DocumentableDto` hierarchy in
 * `dtos/SemanticModelDtos.kt`: that hierarchy mirrors Dokka's own AST, whereas everything here
 * mirrors what a javadoc page actually presents (inheritance closures, inherited-member groups,
 * summary tables, the global index pages). Keeping them separate means Javadoc mode can add
 * javadoc-specific concepts without perturbing the default output's schema.
 *
 * Field naming follows the javadoc page section it comes from, so a consumer holding a javadoc
 * page open next to the JSON can match them up section by section.
 */

// --- Shared building blocks ---

/**
 * A reference to a type. [display] is the javadoc-style rendering including type arguments and
 * array dimensions (e.g. `List<E>`, `int[]`, `? extends Number`); [qualifiedName] and [url] are
 * populated only when the referenced type is part of this documentation run.
 */
@Serializable
data class JdTypeRef(
    val display: String,
    val qualifiedName: String? = null,
    val url: String? = null,
    val kind: String? = null
)

/** A resolved `@see` / `@link` reference. [url] is null when the target isn't documented here. */
@Serializable
data class JdSeeRef(
    val label: String,
    val url: String? = null,
    val qualifiedName: String? = null
)

/** A javadoc block tag this mapper has no dedicated field for (`@apiNote`, `@implSpec`, ...). */
@Serializable
data class JdTag(
    val name: String,
    val text: String
)

@Serializable
data class JdDeprecation(
    val comment: String? = null,
    val forRemoval: Boolean = false,
    val since: String? = null
)

/** A method/constructor parameter, paired with its `@param` text when the source documents one. */
@Serializable
data class JdParameter(
    val name: String,
    val type: JdTypeRef,
    val description: String? = null,
    val annotations: List<String> = emptyList()
)

/** A type parameter declaration, paired with its `@param <T>` text. */
@Serializable
data class JdTypeParameter(
    val name: String,
    val bounds: List<JdTypeRef> = emptyList(),
    val description: String? = null
)

/** One entry of a `@throws`/`@exception` list. */
@Serializable
data class JdThrows(
    val type: JdTypeRef,
    val description: String? = null
)

/**
 * A pointer at another member -- used for javadoc's "Specified by:" / "Overrides:" notes and for
 * the members listed in an inherited-member group.
 */
@Serializable
data class JdMemberRef(
    val name: String,
    val signature: String,
    val url: String? = null,
    val declaringType: JdTypeRef? = null
)

/**
 * One "Methods declared in class X" / "Fields declared in interface Y" group, as javadoc renders
 * them at the bottom of a summary table.
 */
@Serializable
data class JdInheritedMembers(
    val declaringType: JdTypeRef,
    val members: List<JdMemberRef> = emptyList()
)

// --- Members ---

/** A field, an enum constant, or a record component's backing field. */
@Serializable
data class JdField(
    val name: String,
    val anchor: String,
    val modifiers: List<String> = emptyList(),
    val type: JdTypeRef,
    val signature: String,
    val url: String? = null,
    val description: String? = null,
    val firstSentence: String? = null,
    val constantValue: String? = null,
    val since: List<String> = emptyList(),
    val seeAlso: List<JdSeeRef> = emptyList(),
    val deprecated: JdDeprecation? = null,
    val annotations: List<String> = emptyList(),
    val tags: List<JdTag> = emptyList()
)

/**
 * A constructor, a method, or an annotation element. [returnType] is null for constructors;
 * [defaultValue] is populated only for annotation elements that declare a `default`.
 */
@Serializable
data class JdExecutable(
    val name: String,
    val anchor: String,
    val kind: String,
    val modifiers: List<String> = emptyList(),
    val typeParameters: List<JdTypeParameter> = emptyList(),
    val returnType: JdTypeRef? = null,
    val parameters: List<JdParameter> = emptyList(),
    val exceptions: List<JdThrows> = emptyList(),
    val signature: String,
    val url: String? = null,
    val description: String? = null,
    val firstSentence: String? = null,
    val returns: String? = null,
    val specifiedBy: List<JdMemberRef> = emptyList(),
    val overrides: JdMemberRef? = null,
    val since: List<String> = emptyList(),
    val seeAlso: List<JdSeeRef> = emptyList(),
    val deprecated: JdDeprecation? = null,
    val annotations: List<String> = emptyList(),
    val defaultValue: String? = null,
    val tags: List<JdTag> = emptyList()
)

/** A nested type as listed in an enclosing type's "Nested Class Summary". */
@Serializable
data class JdNestedTypeRef(
    val name: String,
    val qualifiedName: String,
    val kind: String,
    val modifiers: List<String> = emptyList(),
    val url: String? = null,
    val firstSentence: String? = null,
    val deprecated: JdDeprecation? = null
)

// --- Pages ---

/** One `<ClassName>.json` page -- the javadoc class/interface/enum/record/annotation page. */
@Serializable
data class JdClassPage(
    val page: String = "class",
    val kind: String,
    val name: String,
    val simpleName: String,
    val qualifiedName: String,
    val packageName: String,
    val moduleName: String? = null,
    /** Link to this type's module page, relative to this page. Null for a non-modular run. */
    val moduleUrl: String? = null,
    /** Link to this type's package page, relative to this page. */
    val packageUrl: String? = null,
    /**
     * This page's own path, relative to the output root. Note the asymmetry with every *link*
     * URL in these DTOs, which is relative to the page it appears on, the way javadoc links are.
     */
    val url: String,
    val modifiers: List<String> = emptyList(),
    val signature: String,
    val typeParameters: List<JdTypeParameter> = emptyList(),
    val superclass: JdTypeRef? = null,
    val superinterfaces: List<JdTypeRef> = emptyList(),
    /** Superclass chain from `java.lang.Object` down to (and including) this type. */
    val inheritance: List<JdTypeRef> = emptyList(),
    val allImplementedInterfaces: List<JdTypeRef> = emptyList(),
    val allSuperinterfaces: List<JdTypeRef> = emptyList(),
    val directKnownSubclasses: List<JdTypeRef> = emptyList(),
    val allKnownSubinterfaces: List<JdTypeRef> = emptyList(),
    val allKnownImplementingClasses: List<JdTypeRef> = emptyList(),
    val enclosingType: JdTypeRef? = null,
    val isFunctionalInterface: Boolean = false,
    val description: String? = null,
    val firstSentence: String? = null,
    val since: List<String> = emptyList(),
    val seeAlso: List<JdSeeRef> = emptyList(),
    val authors: List<String> = emptyList(),
    val versions: List<String> = emptyList(),
    val deprecated: JdDeprecation? = null,
    val annotations: List<String> = emptyList(),
    val tags: List<JdTag> = emptyList(),
    val nestedTypes: List<JdNestedTypeRef> = emptyList(),
    val recordComponents: List<JdParameter> = emptyList(),
    val enumConstants: List<JdField> = emptyList(),
    val fields: List<JdField> = emptyList(),
    val constructors: List<JdExecutable> = emptyList(),
    val methods: List<JdExecutable> = emptyList(),
    /**
     * The elements of an annotation type.
     *
     * javadoc splits these into "Required" and "Optional" tables by whether the element declares
     * a `default`. Dokka's model does not carry annotation-element default values, so making that
     * split here would mean labelling every element "required" whether it is or not; instead they
     * are reported as one list and each element's [JdExecutable.defaultValue] is populated when
     * (and only when) Dokka does supply it.
     */
    val annotationElements: List<JdExecutable> = emptyList(),
    val inheritedNestedTypes: List<JdInheritedMembers> = emptyList(),
    val inheritedFields: List<JdInheritedMembers> = emptyList(),
    val inheritedMethods: List<JdInheritedMembers> = emptyList()
)

/** One entry in a package page's type table, or in `allclasses-index.json`. */
@Serializable
data class JdTypeSummary(
    val name: String,
    val qualifiedName: String,
    val kind: String,
    val packageName: String,
    val moduleName: String? = null,
    val url: String? = null,
    val firstSentence: String? = null,
    val deprecated: JdDeprecation? = null,
    /** The type's own modifiers, so a consumer can index only the public API as javadoc does. */
    val modifiers: List<String> = emptyList()
)

/** One `package-summary.json` page. */
@Serializable
data class JdPackagePage(
    val page: String = "package",
    val name: String,
    val moduleName: String? = null,
    /** Link to this package's module page, relative to this page. Null for a non-modular run. */
    val moduleUrl: String? = null,
    /** This page's own path, relative to the output root -- see [JdClassPage.url]. */
    val url: String,
    val description: String? = null,
    val firstSentence: String? = null,
    val since: List<String> = emptyList(),
    val seeAlso: List<JdSeeRef> = emptyList(),
    val deprecated: JdDeprecation? = null,
    val tags: List<JdTag> = emptyList(),
    /** The parent, child and sibling packages javadoc lists under "Related Packages". */
    val relatedPackages: List<JdPackageSummary> = emptyList(),
    val interfaces: List<JdTypeSummary> = emptyList(),
    val classes: List<JdTypeSummary> = emptyList(),
    val enums: List<JdTypeSummary> = emptyList(),
    val records: List<JdTypeSummary> = emptyList(),
    val exceptions: List<JdTypeSummary> = emptyList(),
    val annotationTypes: List<JdTypeSummary> = emptyList(),
    /** Every type in the package, in one list, regardless of which table above it also appears in. */
    val allTypes: List<JdTypeSummary> = emptyList()
)

/** One entry in a module page's package table, or in `allpackages-index.json`. */
@Serializable
data class JdPackageSummary(
    val name: String,
    val moduleName: String? = null,
    val url: String? = null,
    val firstSentence: String? = null,
    val deprecated: JdDeprecation? = null
)

/** One `requires` directive on a module page. */
@Serializable
data class JdModuleRequires(
    val module: String,
    val isTransitive: Boolean = false,
    val isStatic: Boolean = false,
    val url: String? = null
)

/**
 * One `exports` or `opens` directive. [to] is empty for an unqualified directive; javadoc shows a
 * populated [to] in its "Exported To" / "Opened To" column, and does not document those packages.
 */
@Serializable
data class JdModuleExport(
    val packageName: String,
    val to: List<String> = emptyList(),
    val url: String? = null,
    /** The exported package's summary sentence, which javadoc shows in this table's last column. */
    val firstSentence: String? = null
)

/**
 * One row of a module page's "Indirect Exports" table: a module readable through this one, and the
 * packages it exports.
 */
@Serializable
data class JdIndirectExport(
    val module: String,
    val moduleUrl: String? = null,
    val packages: List<JdPackageSummary> = emptyList()
)

/** One `provides ... with ...` directive. */
@Serializable
data class JdModuleProvides(
    val service: JdTypeRef,
    val implementations: List<JdTypeRef> = emptyList()
)

/**
 * One `module-summary.json` page.
 *
 * The JPMS sections -- [requires], [exports], [opens], [uses], [provides] -- are read from the
 * module's `module-info.java`, which Dokka's own model does not carry (a Dokka "module" is a
 * build-level grouping, not a JPMS module). They are populated whenever the run's source roots
 * are JPMS module roots, and are empty otherwise.
 */
@Serializable
data class JdModulePage(
    val page: String = "module",
    val name: String,
    /** This page's own path, relative to the output root -- see [JdClassPage.url]. */
    val url: String,
    val description: String? = null,
    val firstSentence: String? = null,
    val since: List<String> = emptyList(),
    val seeAlso: List<JdSeeRef> = emptyList(),
    val deprecated: JdDeprecation? = null,
    val tags: List<JdTag> = emptyList(),
    /** The module's documented packages -- those it exports unqualified. */
    val packages: List<JdPackageSummary> = emptyList(),
    val requires: List<JdModuleRequires> = emptyList(),
    /**
     * Modules a consumer of this one also reads, reached through `requires transitive` but not
     * required directly -- javadoc's "Indirect Requires" table.
     */
    val indirectRequires: List<JdModuleRequires> = emptyList(),
    val exports: List<JdModuleExport> = emptyList(),
    /**
     * Packages that become part of this module's API surface because it re-exports the modules
     * providing them -- javadoc's "Indirect Exports" table.
     */
    val indirectExports: List<JdIndirectExport> = emptyList(),
    val opens: List<JdModuleExport> = emptyList(),
    val uses: List<JdTypeRef> = emptyList(),
    val provides: List<JdModuleProvides> = emptyList()
)

/** `index.json` -- javadoc's overview page. */
@Serializable
data class JdOverviewPage(
    val page: String = "overview",
    val title: String? = null,
    val modules: List<JdModuleSummary> = emptyList(),
    val packages: List<JdPackageSummary> = emptyList()
)

@Serializable
data class JdModuleSummary(
    val name: String,
    val url: String? = null,
    val firstSentence: String? = null
)

/** `allclasses-index.json`. */
@Serializable
data class JdAllClassesIndex(
    val page: String = "all-classes",
    val types: List<JdTypeSummary> = emptyList()
)

/** `allpackages-index.json`. */
@Serializable
data class JdAllPackagesIndex(
    val page: String = "all-packages",
    val packages: List<JdPackageSummary> = emptyList()
)

/** One row of `deprecated-list.json`, grouped under the javadoc section it belongs to. */
@Serializable
data class JdDeprecatedEntry(
    val element: String,
    val kind: String,
    val url: String? = null,
    val comment: String? = null,
    val forRemoval: Boolean = false,
    val since: String? = null
)

/** `deprecated-list.json`, keyed by javadoc's section names (`classes`, `methods`, ...). */
@Serializable
data class JdDeprecatedList(
    val page: String = "deprecated-list",
    val sections: Map<String, List<JdDeprecatedEntry>> = emptyMap()
)

@Serializable
data class JdConstantField(
    val name: String,
    val modifiers: List<String> = emptyList(),
    val type: JdTypeRef,
    val value: String,
    val url: String? = null
)

@Serializable
data class JdConstantsForType(
    val qualifiedName: String,
    val url: String? = null,
    val fields: List<JdConstantField> = emptyList()
)

/** `constant-values.json`, grouped by package then by declaring type, as javadoc groups it. */
@Serializable
data class JdConstantValues(
    val page: String = "constant-values",
    val packages: Map<String, List<JdConstantsForType>> = emptyMap()
)

/** One entry of the A-Z index that javadoc splits across `index-files/index-N.html`. */
@Serializable
data class JdIndexEntry(
    val label: String,
    val kind: String,
    val url: String? = null,
    val containingElement: String? = null,
    val firstSentence: String? = null,
    val deprecated: Boolean = false
)

/** One `index-files/index-N.json` page. */
@Serializable
data class JdIndexPage(
    val page: String = "index",
    val letter: String,
    val index: Int,
    val letters: List<String> = emptyList(),
    val entries: List<JdIndexEntry> = emptyList()
)
