package com.example.shapes;

import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/**
 * Marks a type whose measurements have been verified against a reference implementation.
 *
 * @since 1.2
 */
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.TYPE)
public @interface Measured {

    /**
     * The tolerance the measurements were verified to.
     *
     * @return the absolute tolerance
     */
    double tolerance();

    /**
     * Who performed the verification.
     *
     * @return the verifier's name, or the empty string if unrecorded
     */
    String verifiedBy() default "";
}
