TODO @Alex

All items below are done as of 2026-07-02 — see README.md for details.

- [x] Move hardcoded options in JsonRenderer (Documentable type discriminator string, pretty printing, etc.) to be config options
- [x] Remove all references to "alex" (the default log file points to my home directory on my own machine)
- [x] Update plugin name (should not be provided under package my.dokka.plugin, "json-output-plugin" should be changed to kdoc-to-json or something)
- [x] Provide script for building the example library and example usage
- [x] Move output comparison/link validity check scripts into this repository
- [x] Update usage to indicate that a user needs to set a matching Dokka version in *json-output-plugin/build.gradle.kts* (default is version 2.2.0-Beta because that's compatible with the current [kotlin-stdlib-docs build tool](https://github.com/JetBrains/kotlin/tree/master/libraries/tools/kotlin-stdlib-docs)
