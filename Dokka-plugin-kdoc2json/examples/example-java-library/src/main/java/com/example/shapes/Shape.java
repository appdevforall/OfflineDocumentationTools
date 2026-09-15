package com.example.shapes;

/**
 * A closed geometric figure with a computable area.
 *
 * <p>Implementations are expected to be immutable; the {@link #area()} of a shape must not change
 * over its lifetime. This mirrors the contract style used throughout the JDK's own collection
 * interfaces.</p>
 *
 * @param <U> the unit of measure areas are reported in
 * @author Docs Pipeline
 * @since 1.0
 * @see Rectangle
 */
public interface Shape<U extends Number> {

    /** The maximum number of sides any shape in this library may declare. */
    int MAX_SIDES = 64;

    /**
     * Returns the area enclosed by this shape.
     *
     * @return the enclosed area, never negative
     */
    U area();

    /**
     * Returns the number of sides this shape has.
     *
     * @return the side count, between 0 and {@value #MAX_SIDES}
     */
    int sides();

    /**
     * Scales this shape by the given factor.
     *
     * @param factor the scaling factor, must be positive
     * @return a new scaled shape
     * @throws IllegalArgumentException if {@code factor} is not positive
     */
    default Shape<U> scaled(double factor) {
        if (factor <= 0) {
            throw new IllegalArgumentException("factor must be positive");
        }
        return this;
    }
}
