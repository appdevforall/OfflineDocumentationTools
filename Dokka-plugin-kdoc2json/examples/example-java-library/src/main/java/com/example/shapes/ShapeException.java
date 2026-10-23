package com.example.shapes;

/**
 * Thrown when a shape cannot be constructed from the supplied measurements.
 *
 * @since 1.0
 */
public class ShapeException extends IllegalArgumentException {

    private static final long serialVersionUID = 1L;

    /**
     * Creates an exception with the given detail message.
     *
     * @param message the detail message
     */
    public ShapeException(String message) {
        super(message);
    }
}
