package com.example.testlib

import java.util.Date

/**
 * A container restricted to naturally-ordered values.
 *
 * Exercises a bounded generic type parameter (`T : Comparable<T>`).
 */
class BoundedContainer<T : Comparable<T>>(val value: T) {
    fun isGreaterThan(other: T): Boolean = value > other
}

/**
 * Copies every element of [source] into [destination].
 *
 * Exercises use-site variance: `out Number` on the read-only source and
 * `in Number` on the mutable destination.
 */
fun copyItems(source: List<out Number>, destination: MutableList<in Number>) {
    destination.addAll(source)
}

/**
 * Returns [value], or `"N/A"` if it is `null`.
 *
 * Exercises a nullable Kotlin type (`String?`).
 */
fun formatOrDefault(value: String?): String = value ?: "N/A"

/**
 * Returns the current time as a Java platform type.
 *
 * Exercises Java interop (`java.util.Date`).
 */
fun currentJavaDate(): Date = Date()

/**
 * The number of whitespace-separated words in this string.
 *
 * Exercises an extension property.
 */
val String.wordCount: Int
    get() = trim().split("\\s+".toRegex()).filter { it.isNotEmpty() }.size

/**
 * Returns this string, uppercased and with an exclamation mark appended.
 *
 * Exercises an extension function.
 */
fun String.shout(): String = uppercase() + "!"

/**
 * Doubles [value] after a (simulated) suspension point.
 *
 * Exercises a suspend function.
 */
suspend fun computeAfterPause(value: Int): Int {
    return value * 2
}

/**
 * Adds [other] to this integer.
 *
 * Exercises an infix function.
 */
infix fun Int.addTo(other: Int): Int = this + other

/**
 * A simple 2D integer vector.
 *
 * Exercises an operator function (`plus`).
 */
data class Vector2(val x: Int, val y: Int) {
    operator fun plus(other: Vector2): Vector2 = Vector2(x + other.x, y + other.y)
}

/**
 * Greets [name].
 *
 * Exercises a function with a default parameter value.
 */
fun greet(name: String, greeting: String = "Hello"): String = "$greeting, $name!"

/**
 * Attaches a name and priority to a declaration. Can be applied more than once
 * to the same declaration.
 *
 * Exercises a multi-parameter, repeatable annotation.
 */
@Repeatable
annotation class Tag(val name: String, val priority: Int = 0)

/**
 * A declaration tagged more than once.
 *
 * Exercises an annotation applied multiple times to the same target.
 */
@Tag("first", priority = 1)
@Tag("second", priority = 2)
class TaggedThing

/**
 * Echoes [value] back.
 *
 * Exercises an annotation applied to a function parameter.
 */
fun annotatedParam(@Tag("param-tag") value: Int): Int = value

/**
 * An immutable 2D point.
 *
 * Exercises a data class.
 */
data class Point(val x: Int, val y: Int)

/**
 * A shape with a computable area.
 *
 * Exercises a sealed class hierarchy.
 */
sealed class Shape {
    abstract fun area(): Double

    /**
     * A circular [Shape].
     */
    data class Circle(val radius: Double) : Shape() {
        override fun area(): Double = Math.PI * radius * radius
    }

    /**
     * A rectangular [Shape].
     */
    data class Rectangle(val width: Double, val height: Double) : Shape() {
        override fun area(): Double = width * height
    }
}

/**
 * A named configuration, only buildable through its companion.
 *
 * Exercises a companion object with real members.
 */
class Configuration private constructor(val name: String) {
    companion object {
        const val DEFAULT_NAME = "default"

        fun createDefault(): Configuration = Configuration(DEFAULT_NAME)
    }
}

/**
 * A value class identified purely by [id].
 *
 * Exercises overriding `equals`, `hashCode`, and `toString`.
 */
class CustomEquals(val id: Int) {
    override fun equals(other: Any?): Boolean = other is CustomEquals && other.id == id
    override fun hashCode(): Int = id
    override fun toString(): String = "CustomEquals(id=$id)"
}

/**
 * A custom checked-style exception.
 *
 * Exercises a class implementing `Throwable`.
 */
class CustomException(message: String) : Throwable(message)

/**
 * Divides [numerator] by [denominator].
 *
 * Supports raw markup in prose, e.g. `1 < 2` and `A & B`, to verify HTML escaping.
 *
 * Formatting check: **bold**, *italic*, and a list:
 * - first item
 * - second item
 *
 * @param numerator the value to divide
 * @param denominator the value to divide by, must not be zero
 * @return the quotient as a [Double]
 * @throws ArithmeticException if [denominator] is zero
 * @see [CustomException]
 * @sample com.example.testlib.safeDivideSample
 * @sample com.example.testlib.safeDivideSampleAlternate
 */
fun safeDivide(numerator: Int, denominator: Int): Double {
    if (denominator == 0) throw ArithmeticException("Division by zero")
    return numerator.toDouble() / denominator
}

/**
 * Runnable sample referenced by the first `@sample` tag on [safeDivide].
 */
fun safeDivideSample() {
    val result = safeDivide(10, 2)
    println(result)
}

/**
 * Runnable sample referenced by the second `@sample` tag on [safeDivide], used to
 * verify multiple `@sample` tags each pull their own source rather than repeating
 * the first one.
 */
fun safeDivideSampleAlternate() {
    val result = safeDivide(7, 3)
    println(result)
}
