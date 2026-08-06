#!/usr/bin/env bash
# Verifies TEST_PLAN.md §7: LinkPostProcessor cross-module resolution.
#
# Not covered: "relative path depth" for a genuinely cross-module-resolved DRI,
# and "last-writer-wins" for expect/actual -- both need a real multi-module or
# multiplatform build. Confirmed empirically that this single-module fixture
# makes LinkPostProcessor's pass-2 replace step resolve exactly 0 links (every
# "unresolved:" marker here is either resolved directly by locationProvider at
# write time, or is permanently unresolvable). Deferred to TEST_PLAN.md §8's
# kotlin-stdlib stress test, per the user's decision when step 3 started.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

publish_plugin
run_dokka '{ "omitNulls": false, "replaceHtmlExtension": true }'

python3 "$TESTS_DIR/helpers/check_link_postprocessor.py" "$OUTPUT_DIR" "$LAST_GRADLE_LOG"
