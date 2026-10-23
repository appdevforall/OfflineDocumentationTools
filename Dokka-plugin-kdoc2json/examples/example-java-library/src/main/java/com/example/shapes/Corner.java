package com.example.shapes;

/**
 * The four corners of an axis-aligned bounding box.
 *
 * @since 1.0
 */
public enum Corner {

    /** The top-left corner. */
    TOP_LEFT,

    /** The top-right corner. */
    TOP_RIGHT,

    /** The bottom-left corner. */
    BOTTOM_LEFT,

    /** The bottom-right corner. */
    BOTTOM_RIGHT;

    /**
     * Returns the corner diagonally opposite this one.
     *
     * @return the opposite corner
     */
    public Corner opposite() {
        switch (this) {
            case TOP_LEFT: return BOTTOM_RIGHT;
            case TOP_RIGHT: return BOTTOM_LEFT;
            case BOTTOM_LEFT: return TOP_RIGHT;
            default: return TOP_LEFT;
        }
    }
}
