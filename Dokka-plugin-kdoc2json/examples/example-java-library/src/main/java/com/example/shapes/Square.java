package com.example.shapes;

/**
 * A rectangle whose sides are all equal.
 *
 * @since 1.1
 */
public class Square extends Rectangle {

    /**
     * Creates a square with the given side length.
     *
     * @param side the length of each side
     */
    public Square(double side) {
        super(side, side);
    }
}
