package com.example.emptypkg

/**
 * Not public, so Dokka's default `documentedVisibilities = [PUBLIC]` excludes it.
 * Exists purely so this package has a source file but zero documented declarations,
 * exercising the plugin's handling of an empty/near-empty module (TEST_PLAN.md §9).
 */
internal class InternalHelper {
    fun doNothing() {}
}
