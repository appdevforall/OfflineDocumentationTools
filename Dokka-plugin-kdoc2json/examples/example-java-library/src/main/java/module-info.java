/**
 * Defines a small geometry library.
 *
 * <p>Present so the plugin's Javadoc mode has a real {@code module-info.java} to read: the module
 * page's requires / exports / uses / provides sections come from here, not from Dokka's model.</p>
 *
 * @uses com.example.shapes.spi.ShapeFactory
 * @since 1.0
 */
module com.example.shapes {
    requires transitive java.logging;
    requires static java.sql;

    exports com.example.shapes;
    exports com.example.shapes.spi;

    uses com.example.shapes.spi.ShapeFactory;
}
