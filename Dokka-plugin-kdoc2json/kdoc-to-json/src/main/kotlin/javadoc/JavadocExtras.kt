package org.appdevforall.dokka.kdoc2json.javadoc

import org.jetbrains.dokka.model.Documentable
import org.jetbrains.dokka.model.properties.PropertyContainer
import org.jetbrains.dokka.model.properties.WithExtraProperties

/**
 * Reads a documentable's extras without caring which concrete subtype it is.
 *
 * Dokka declares `extra` on [WithExtraProperties] rather than on [Documentable], so every caller
 * would otherwise need its own cast; the star projection is safe here because extras are only ever
 * read, never added.
 */
internal fun Documentable.extrasOrEmpty(): PropertyContainer<*> =
    (this as? WithExtraProperties<*>)?.extra ?: PropertyContainer.empty<Documentable>()
