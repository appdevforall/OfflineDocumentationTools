package com.example.shapes;

/**
 * An axis-aligned rectangle measured in {@code double} units.
 *
 * <p>Example:</p>
 * <pre>{@code
 * Rectangle r = new Rectangle(3.0, 4.0);
 * assert r.area() == 12.0;
 * }</pre>
 *
 * @since 1.0
 * @see Shape
 */
public class Rectangle extends AbstractShape<Double> {

    /** A rectangle of zero width and height. */
    public static final String EMPTY_LABEL = "empty";

    /** The number of sides a rectangle always has. */
    public static final int SIDE_COUNT = 4;

    private final double width;
    private final double height;

    /**
     * Creates a rectangle of the given dimensions.
     *
     * @param width the width, must not be negative
     * @param height the height, must not be negative
     * @throws IllegalArgumentException if either dimension is negative
     */
    public Rectangle(double width, double height) {
        super("rectangle");
        if (width < 0 || height < 0) {
            throw new IllegalArgumentException("dimensions must not be negative");
        }
        this.width = width;
        this.height = height;
    }

    /** Creates a unit square. */
    public Rectangle() {
        this(1.0, 1.0);
    }

    @Override
    public Double area() {
        return width * height;
    }

    /**
     * Returns the rectangle's width.
     *
     * @return the width in unspecified units
     */
    public double getWidth() {
        return width;
    }

    /**
     * Returns the rectangle's height.
     *
     * @return the height in unspecified units
     */
    public double getHeight() {
        return height;
    }

    /**
     * Returns the perimeter.
     *
     * @return twice the sum of width and height
     * @deprecated Use {@link #getWidth()} and {@link #getHeight()} and compute it directly.
     *     Scheduled for removal in 3.0.
     */
    @Deprecated(since = "2.0", forRemoval = true)
    public double perimeter() {
        return 2 * (width + height);
    }

    /**
     * A builder for {@link Rectangle} instances.
     *
     * <p>Nested to exercise javadoc's nested-type sections.</p>
     */
    public static final class Builder {
        private double width;
        private double height;

        /**
         * Sets the width.
         *
         * @param width the width
         * @return this builder
         */
        public Builder width(double width) {
            this.width = width;
            return this;
        }

        /**
         * Sets the height.
         *
         * @param height the height
         * @return this builder
         */
        public Builder height(double height) {
            this.height = height;
            return this;
        }

        /**
         * Builds the rectangle.
         *
         * @return a new rectangle
         */
        public Rectangle build() {
            return new Rectangle(width, height);
        }
    }
}
