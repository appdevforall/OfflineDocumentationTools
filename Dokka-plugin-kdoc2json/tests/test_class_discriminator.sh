#!/usr/bin/env bash
# Verifies the `classDiscriminator` config option renames the polymorphic type key
# (default "kind") to whatever key name is configured.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

SAMPLE="$OUTPUT_DIR/com.example.utils/-data-processor/index.json"

publish_plugin

echo "=== classDiscriminator=\"kind\" (default): the \"kind\" key is used ==="
run_dokka '{ "classDiscriminator": "kind" }'
assert_contains "$SAMPLE" '"kind":"class"' "default discriminator key \"kind\" present"

echo "=== classDiscriminator=\"elementType\": key renamed, old \"kind\" key gone ==="
run_dokka '{ "classDiscriminator": "elementType" }'
assert_not_contains "$SAMPLE" '"kind"' "old \"kind\" key no longer present"
assert_contains "$SAMPLE" '"elementType":"class"' "custom discriminator key \"elementType\" used instead"

summarize_and_exit
