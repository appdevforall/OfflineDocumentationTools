#!/usr/bin/env bash
# Generates the JDK's API documentation as javadoc-shaped JSON, mirroring the api/ tree of the
# official docs (the ones in SourceDocs/JavaDocs/html/api).
#
# Three steps:
#   1. stage_jdk_sources.py unpacks the JDK's lib/src.zip and keeps only what javadoc documents:
#      one directory per JPMS module, containing that module's unqualified-exported packages.
#   2. jdk-docs/ runs Dokka over that tree with kdoc-to-json in javadoc-mode.
#   3. The result is copied to the output directory.
#
# The plugin reads each module's module-info.java back out of the staging tree, which is what
# gives the output its <module>/<package>/<Class>.json layout and fills in the module pages'
# requires / exports / uses / provides sections.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PLUGIN_DIR="$ROOT_DIR/kdoc-to-json"
PROJECT_DIR="$SCRIPT_DIR/jdk-docs"

usage() {
    cat >&2 <<USAGE
Usage: $0 [-j <jdk-home>] [-o <output-dir>] [-w <work-dir>] [-m <modules>]
          [--dokka-worker-heap <size>] [--skip-publish]

  -j  JDK whose lib/src.zip to document. Defaults to \$JDK_SOURCE_HOME, else \$JAVA_HOME.
      Use a JDK matching the docs you want to reproduce -- the docs under
      SourceDocs/JavaDocs are Java SE 17.
  -o  Where to write the JSON tree. Default: $SCRIPT_DIR/build-output/api
  -w  Scratch directory for the extracted and staged sources.
      Default: $SCRIPT_DIR/build-output/work
  -m  Comma-separated module names to document instead of all of them. Useful for a quick
      check: -m java.sql,java.transaction.xa takes seconds rather than many minutes.
  --dokka-worker-heap
      Max heap for Dokka's *worker* process, e.g. 6g. Forwarded to Gradle as
      -PdokkaWorkerHeap, which jdk-docs/build.gradle.kts reads. It has to be a
      real Gradle command-line property: the worker is not the Gradle daemon,
      so neither GRADLE_OPTS nor org.gradle.jvmargs sizes it, and passing it
      through the environment instead fails silently -- the build just runs at
      the 24g default and dies wherever that is too much. Default: unset, so
      build.gradle.kts's own default applies.
  --skip-publish  Don't republish kdoc-to-json to mavenLocal first.
USAGE
    exit 1
}

JDK_HOME="${JDK_SOURCE_HOME:-${JAVA_HOME:-}}"
OUTPUT_DIR="$SCRIPT_DIR/build-output/api"
WORK_DIR="$SCRIPT_DIR/build-output/work"
MODULES=""
DOKKA_WORKER_HEAP=""
SKIP_PUBLISH=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -j) JDK_HOME="$2"; shift 2 ;;
        -o) OUTPUT_DIR="$2"; shift 2 ;;
        -w) WORK_DIR="$2"; shift 2 ;;
        -m) MODULES="$2"; shift 2 ;;
        --dokka-worker-heap) DOKKA_WORKER_HEAP="$2"; shift 2 ;;
        --skip-publish) SKIP_PUBLISH=1; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $1" >&2; usage ;;
    esac
done

if [[ -z "$JDK_HOME" ]]; then
    echo "Error: no JDK given. Pass -j <jdk-home>, or set JDK_SOURCE_HOME or JAVA_HOME." >&2
    exit 1
fi

SRC_ZIP="$JDK_HOME/lib/src.zip"
if [[ ! -f "$SRC_ZIP" ]]; then
    echo "Error: $SRC_ZIP not found -- that JDK doesn't ship sources." >&2
    exit 1
fi

STAGING_DIR="$WORK_DIR/staged"
EXTRACT_DIR="$WORK_DIR/src-extracted"
mkdir -p "$WORK_DIR"

echo "==> JDK sources: $SRC_ZIP"
"$JDK_HOME/bin/java" -version 2>&1 | head -1 | sed 's/^/    /'

stage_args=("$SRC_ZIP" "$STAGING_DIR" --extract-to "$EXTRACT_DIR")
if [[ -n "$MODULES" ]]; then
    stage_args+=(--modules "$MODULES")
fi
python3 "$SCRIPT_DIR/stage_jdk_sources.py" "${stage_args[@]}"

if [[ "$SKIP_PUBLISH" != "1" ]]; then
    echo "==> Publishing kdoc-to-json to mavenLocal"
    (cd "$PLUGIN_DIR" && ./gradlew --console=plain -q publishToMavenLocal)
fi

echo "==> Running Dokka in javadoc-mode over the staged sources"
echo "    (the whole JDK is ~4,800 files across 60 modules; this takes a while)"
gradle_args=(--console=plain dokkaGenerate -PjdkSources="$STAGING_DIR")
if [[ -n "$DOKKA_WORKER_HEAP" ]]; then
    gradle_args+=(-PdokkaWorkerHeap="$DOKKA_WORKER_HEAP")
    echo "    Dokka worker heap: $DOKKA_WORKER_HEAP"
fi
(cd "$PROJECT_DIR" && ./gradlew "${gradle_args[@]}")

GENERATED="$PROJECT_DIR/build/dokka/html"
if [[ ! -d "$GENERATED" ]]; then
    echo "Error: Dokka produced no output at $GENERATED" >&2
    exit 1
fi

# Dokka only writes JSON, so the staged doc-files/ directories have to be carried across
# separately. The HTML renderer copies every non-JSON file through untouched, so putting them in
# the JSON tree is enough to get them into the rendered output too.
echo "==> Copying doc-files/ alongside the generated JSON"
doc_file_count=0
while IFS= read -r dir; do
    rel="${dir#"$STAGING_DIR"/}"
    mkdir -p "$GENERATED/$rel"
    cp -R "$dir"/. "$GENERATED/$rel"/
    doc_file_count=$((doc_file_count + 1))
done < <(find "$STAGING_DIR" -type d -name doc-files)
echo "    $doc_file_count doc-files directory/ies"

echo "==> Copying output to $OUTPUT_DIR"
rm -rf "$OUTPUT_DIR"
mkdir -p "$(dirname "$OUTPUT_DIR")"
cp -R "$GENERATED" "$OUTPUT_DIR"

json_count=$(find "$OUTPUT_DIR" -name '*.json' | wc -l | tr -d ' ')
module_count=$(find "$OUTPUT_DIR" -name 'module-summary.json' | wc -l | tr -d ' ')
package_count=$(find "$OUTPUT_DIR" -name 'package-summary.json' | wc -l | tr -d ' ')

echo
echo "Done: $json_count JSON files -- $module_count modules, $package_count packages."
echo "  Output:     $OUTPUT_DIR"
echo "  Plugin log: $PROJECT_DIR/build/dokka_json.log"
echo
echo "Compare against the official docs with:"
echo "  python3 $SCRIPT_DIR/compare_with_javadoc.py $OUTPUT_DIR <path-to>/SourceDocs/JavaDocs/html/api"
