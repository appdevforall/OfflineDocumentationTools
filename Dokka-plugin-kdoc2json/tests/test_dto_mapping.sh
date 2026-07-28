#!/usr/bin/env bash
# Verifies TEST_PLAN.md §2: DTO mapping correctness (ModelMapper.mapToDto)
# against the example-data-processor fixture (including Advanced.kt from
# step 1). Structural JSON checks live in helpers/check_dto_mapping.py since
# they need real parsing, not grep.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

publish_plugin
run_dokka '{ "omitNulls": false, "replaceHtmlExtension": true }'

python3 "$TESTS_DIR/helpers/check_dto_mapping.py" "$OUTPUT_DIR"
