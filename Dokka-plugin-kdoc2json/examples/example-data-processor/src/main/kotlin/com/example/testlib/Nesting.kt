package com.example.testlib

/**
 * A singleton object to fulfill the `DObject` requirement.
 * This acts as the root of our deep hierarchy.
 */
object Level1 {
    
    /**
     * A nested class to fulfill the `DClass` requirement.
     * * **Hierarchy Depth 1:** The breadcrumbs for this class will be `Overview / com.example.testlib / Level1 / Level2`.
     * **Internal Link 3:** Implements the [Provider] interface.
     */
    @Meta("Deeply nested class")
    class Level2 : Provider<DataMap> {
        
        override val data: DataMap = emptyMap()

        override fun process(input: DataMap): DataMap {
            return input
        }

        /**
         * An enum class to fulfill the `DEnum` requirement.
         * * **Hierarchy Depth 2:** The breadcrumbs for this enum will be `Overview / com.example.testlib / Level1 / Level2 / Level3`.
         * This successfully proves deep hierarchy rendering.
         */
        enum class Level3 {
            /**
             * Enum entry to fulfill the `DEnumEntry` requirement.
             */
            ACTIVE, 
            
            /**
             * Enum entry to fulfill the `DEnumEntry` requirement.
             */
            INACTIVE
        }
    }
}