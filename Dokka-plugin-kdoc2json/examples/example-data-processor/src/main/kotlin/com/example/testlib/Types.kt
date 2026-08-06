package com.example.testlib

/**
 * A generic map of string keys to arbitrary values, used as the payload type for [Provider].
 * Exists to exercise `DTypeAlias` rendering in the JSON output.
 */
typealias DataMap = Map<String, Any>

/**
 * A generic contract for types that expose and transform a data payload.
 * Exists to exercise `DInterface` rendering in the JSON output.
 */
interface Provider<T> {
    val data: T
    fun process(input: T): T
}

/**
 * Attaches an arbitrary human-readable description to a declaration.
 * Exists to exercise `DAnnotation` rendering in the JSON output.
 */
annotation class Meta(val description: String)
