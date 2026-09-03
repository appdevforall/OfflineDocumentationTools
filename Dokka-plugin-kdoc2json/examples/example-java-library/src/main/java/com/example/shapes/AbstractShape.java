package com.example.shapes;

/**
 * Skeletal implementation of {@link Shape} that supplies the parts every shape shares.
 *
 * @param <U> the unit of measure areas are reported in
 * @since 1.0
 */
public abstract class AbstractShape<U extends Number> implements Shape<U> {

    /** Identifies this shape for diagnostics; never {@code null}. */
    protected final String name;

    /**
     * Creates a shape with the given diagnostic name.
     *
     * @param name the shape's name
     */
    protected AbstractShape(String name) {
        this.name = name;
    }

    /**
     * {@inheritDoc}
     *
     * <p>This implementation always reports four sides.</p>
     */
    @Override
    public int sides() {
        return 4;
    }

    /**
     * Returns this shape's diagnostic name.
     *
     * @return the name passed to the constructor
     */
    public String getName() {
        return name;
    }

    @Override
    public String toString() {
        return name + "[" + area() + "]";
    }
}
