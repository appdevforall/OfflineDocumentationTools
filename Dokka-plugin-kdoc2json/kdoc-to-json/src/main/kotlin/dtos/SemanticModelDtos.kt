package my.dokka.plugin.dtos

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

// --- Documentation Tags ---

@Serializable
data class TagWrapperDto(
    val type: String,
    val text: String,
    val name: String? = null
)

// --- Extras & Properties ---

@Serializable
data class AnnotationWrapperDto(
    val dri: String,
    val params: Map<String, String>,
    val url: String? = null
)

@Serializable
data class ExtrasDto(
    val annotations: Map<String, List<AnnotationWrapperDto>> = emptyMap(),
    val defaultValues: Map<String, String> = emptyMap(),
    val additionalModifiers: Map<String, List<String>> = emptyMap(),
    val isObviousMember: Boolean = false,
    val isException: Boolean = false
)

// --- Bounds & Projections (Types) ---

@Serializable
sealed class ProjectionDto

@Serializable
@SerialName("Star")
object StarDto : ProjectionDto()

@Serializable
sealed class VarianceDto : ProjectionDto() {
    abstract val inner: BoundDto
}
@Serializable @SerialName("Covariance") data class CovarianceDto(override val inner: BoundDto) : VarianceDto()
@Serializable @SerialName("Contravariance") data class ContravarianceDto(override val inner: BoundDto) : VarianceDto()
@Serializable @SerialName("Invariance") data class InvarianceDto(override val inner: BoundDto) : VarianceDto()

@Serializable
sealed class BoundDto : ProjectionDto() {
    abstract val url: String?
}

@Serializable @SerialName("TypeParameter") 
data class TypeParameterBoundDto(val dri: String, val name: String, val presentableName: String?, override val url: String? = null) : BoundDto()

@Serializable @SerialName("Nullable") 
data class NullableDto(val inner: BoundDto, override val url: String? = null) : BoundDto()

@Serializable @SerialName("DefinitelyNonNullable") 
data class DefinitelyNonNullableDto(val inner: BoundDto, override val url: String? = null) : BoundDto()

@Serializable @SerialName("TypeAliased") 
data class TypeAliasedDto(val typeAlias: BoundDto, val inner: BoundDto, override val url: String? = null) : BoundDto()

@Serializable @SerialName("PrimitiveJavaType") 
data class PrimitiveJavaTypeDto(val name: String, override val url: String? = null) : BoundDto()

@Serializable @SerialName("JavaObject") data class JavaObjectDto(override val url: String? = null) : BoundDto()
@Serializable @SerialName("Void") data class VoidDto(override val url: String? = null) : BoundDto()
@Serializable @SerialName("Dynamic") data class DynamicDto(override val url: String? = null) : BoundDto()

@Serializable @SerialName("UnresolvedBound") 
data class UnresolvedBoundDto(val name: String, override val url: String? = null) : BoundDto()

@Serializable @SerialName("GenericTypeConstructor") 
data class GenericTypeConstructorDto(
    val dri: String, 
    val projections: List<ProjectionDto>, 
    val presentableName: String?,
    override val url: String? = null
) : BoundDto()

@Serializable @SerialName("FunctionalTypeConstructor") 
data class FunctionalTypeConstructorDto(
    val dri: String, 
    val projections: List<ProjectionDto>, 
    val isExtensionFunction: Boolean, 
    val isSuspendable: Boolean, 
    val presentableName: String?,
    override val url: String? = null
) : BoundDto()

@Serializable
data class TypeConstructorWithKindDto(
    val typeConstructor: BoundDto, 
    val kind: String
)

// --- Primary Documentables ---

@Serializable
sealed class DocumentableDto {
    abstract val dri: String
    abstract val name: String?
    abstract val url: String?
    abstract val documentation: Map<String, List<TagWrapperDto>>
    abstract val sourceSets: List<String>
    abstract val expectPresentInSet: String?
    abstract val extras: ExtrasDto
    abstract val breadcrumbs: List<BreadcrumbNode>
}

@Serializable
@SerialName("module")
data class ModuleDto(
    override val dri: String,
    override val name: String,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val packages: List<DocumentableDto>
) : DocumentableDto()

@Serializable
@SerialName("package")
data class PackageDto(
    override val dri: String,
    override val name: String,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val functions: List<DocumentableDto>,
    val properties: List<DocumentableDto>,
    val classlikes: List<DocumentableDto>,
    val typeAliases: List<DocumentableDto>
) : DocumentableDto()

@Serializable
@SerialName("class")
data class ClassDto(
    override val dri: String,
    override val name: String,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val constructors: List<DocumentableDto>,
    val functions: List<DocumentableDto>,
    val properties: List<DocumentableDto>,
    val classlikes: List<DocumentableDto>,
    val sources: Map<String, String>,
    val visibility: Map<String, String>,
    val companion: ObjectDto?,
    val generics: List<TypeParameterDto>,
    val supertypes: Map<String, List<TypeConstructorWithKindDto>>,
    val modifier: Map<String, String>,
    val isExpectActual: Boolean,
    val typealiases: List<DocumentableDto>
) : DocumentableDto()

@Serializable
@SerialName("enum")
data class EnumDto(
    override val dri: String,
    override val name: String,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val entries: List<DocumentableDto>,
    val constructors: List<DocumentableDto>,
    val functions: List<DocumentableDto>,
    val properties: List<DocumentableDto>,
    val classlikes: List<DocumentableDto>,
    val sources: Map<String, String>,
    val visibility: Map<String, String>,
    val companion: ObjectDto?,
    val supertypes: Map<String, List<TypeConstructorWithKindDto>>,
    val isExpectActual: Boolean,
    val typealiases: List<DocumentableDto>
) : DocumentableDto()

@Serializable
@SerialName("enumEntry")
data class EnumEntryDto(
    override val dri: String,
    override val name: String,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val functions: List<DocumentableDto>,
    val properties: List<DocumentableDto>,
    val classlikes: List<DocumentableDto>
) : DocumentableDto()

@Serializable
@SerialName("function")
data class FunctionDto(
    override val dri: String,
    override val name: String,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val isConstructor: Boolean,
    val parameters: List<ParameterDto>,
    val sources: Map<String, String>,
    val visibility: Map<String, String>,
    val type: BoundDto,
    val generics: List<TypeParameterDto>,
    val receiver: ParameterDto?,
    val modifier: Map<String, String>,
    val isExpectActual: Boolean,
    val contextParameters: List<ParameterDto>
) : DocumentableDto()

@Serializable
@SerialName("interface")
data class InterfaceDto(
    override val dri: String,
    override val name: String,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val functions: List<DocumentableDto>,
    val properties: List<DocumentableDto>,
    val classlikes: List<DocumentableDto>,
    val sources: Map<String, String>,
    val visibility: Map<String, String>,
    val companion: ObjectDto?,
    val generics: List<TypeParameterDto>,
    val supertypes: Map<String, List<TypeConstructorWithKindDto>>,
    val modifier: Map<String, String>,
    val isExpectActual: Boolean,
    val typealiases: List<DocumentableDto>
) : DocumentableDto()

@Serializable
@SerialName("object")
data class ObjectDto(
    override val dri: String,
    override val name: String?,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val functions: List<DocumentableDto>,
    val properties: List<DocumentableDto>,
    val classlikes: List<DocumentableDto>,
    val sources: Map<String, String>,
    val visibility: Map<String, String>,
    val supertypes: Map<String, List<TypeConstructorWithKindDto>>,
    val isExpectActual: Boolean,
    val typealiases: List<DocumentableDto>
) : DocumentableDto()

@Serializable
@SerialName("annotation")
data class AnnotationDto(
    override val dri: String,
    override val name: String,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val functions: List<DocumentableDto>,
    val properties: List<DocumentableDto>,
    val classlikes: List<DocumentableDto>,
    val sources: Map<String, String>,
    val visibility: Map<String, String>,
    val companion: ObjectDto?,
    val constructors: List<DocumentableDto>,
    val generics: List<TypeParameterDto>,
    val isExpectActual: Boolean
) : DocumentableDto()

@Serializable
@SerialName("property")
data class PropertyDto(
    override val dri: String,
    override val name: String,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val sources: Map<String, String>,
    val visibility: Map<String, String>,
    val type: BoundDto,
    val receiver: ParameterDto?,
    val setter: FunctionDto?,
    val getter: FunctionDto?,
    val modifier: Map<String, String>,
    val generics: List<TypeParameterDto>,
    val isExpectActual: Boolean,
    val contextParameters: List<ParameterDto>
) : DocumentableDto()

@Serializable
@SerialName("parameter")
data class ParameterDto(
    override val dri: String,
    override val name: String?,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val type: BoundDto
) : DocumentableDto()

@Serializable
@SerialName("typeParameter")
data class TypeParameterDto(
    override val dri: String,
    override val name: String,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val bounds: List<BoundDto>,
    val variantTypeParameter: VarianceDto
) : DocumentableDto()

@Serializable
@SerialName("typeAlias")
data class TypeAliasDto(
    override val dri: String,
    override val name: String,
    override val url: String?,
    override val documentation: Map<String, List<TagWrapperDto>>,
    override val sourceSets: List<String>,
    override val expectPresentInSet: String?,
    override val extras: ExtrasDto,
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val type: BoundDto,
    val underlyingType: Map<String, BoundDto>,
    val visibility: Map<String, String>,
    val generics: List<TypeParameterDto>,
    val sources: Map<String, String>
) : DocumentableDto()

// --- Multimodule References ---

@Serializable
data class ModuleReferenceDto(
    val name: String,
    val url: String
)

@Serializable
@SerialName("multimoduleRoot")
data class MultimoduleRootDto(
    override val dri: String = "multimodule-root",
    override val name: String?,
    override val url: String? = null,
    override val documentation: Map<String, List<TagWrapperDto>> = emptyMap(),
    override val sourceSets: List<String> = emptyList(),
    override val expectPresentInSet: String? = null,
    override val extras: ExtrasDto = ExtrasDto(),
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val modules: List<ModuleReferenceDto>
) : DocumentableDto()

@Serializable
data class BreadcrumbNode(
    val name: String,
    val url: String?
)

// --- NEW: All Types Index ---

@Serializable
data class TypeIndexEntryDto(
    val name: String,
    val kind: String,
    val dri: String,
    val url: String?,
    val sourceSets: List<String>
)

@Serializable
@SerialName("allTypes")
data class AllTypesDto(
    override val dri: String = "all-types",
    override val name: String? = "All Types",
    override val url: String? = "all-types.json",
    override val documentation: Map<String, List<TagWrapperDto>> = emptyMap(),
    override val sourceSets: List<String> = emptyList(),
    override val expectPresentInSet: String? = null,
    override val extras: ExtrasDto = ExtrasDto(),
    override val breadcrumbs: List<BreadcrumbNode> = emptyList(),
    val types: List<TypeIndexEntryDto>
) : DocumentableDto()