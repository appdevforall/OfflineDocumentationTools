package com.example.shapes.spi;

import com.example.shapes.Shape;

/**
 * Service provider interface for creating shapes from a textual specification.
 *
 * <p>Exists in a second package so Javadoc mode's package tables and cross-package links have
 * something to resolve.</p>
 *
 * @since 1.2
 */
public interface ShapeFactory {

    /**
     * Parses a shape from its textual form.
     *
     * @param specification the shape specification
     * @return the parsed shape
     * @throws java.text.ParseException if the specification is malformed
     */
    Shape<Double> parse(String specification) throws java.text.ParseException;
}
