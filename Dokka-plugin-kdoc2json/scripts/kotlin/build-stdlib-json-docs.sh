#!/usr/bin/env bash
# Builds the kotlin-stdlib/kotlin-test/kotlin-reflect API docs as JSON via the
# kdoc-to-json Dokka plugin, against a full kotlin/ (https://github.com/JetBrains/kotlin)
# repo checkout - freshly compiling and publishing the plugin from source
# first, so every run picks up whatever's currently in
# Dokka-plugin-kdoc2json/kdoc-to-json/src, not a jar left over from an
# earlier run.
#
# Only generates the JSON output (dokkaGenerateModuleJson), not the default
# HTML - JSON/latest/all-libs is the only thing this project's pipeline
# (sync_kdoc_json_to_db.py) consumes. Use build-kotlin-stdlib.sh directly,
# against libraries/tools/kotlin-stdlib-docs, if you also want the HTML
# comparison output that test_kotlin_stdlib.sh checks against.
#
# The target kotlin-stdlib-docs project's build.gradle.kts is swapped out
# for this directory's own (JSON-plugin-enabled) copy for the duration of
# the build, then restored automatically on exit - the kotlin checkout is
# left exactly as it was found, whether the build succeeds or fails.
#
# kotlin_big (a subproject of kotlin-stdlib-docs) extracts the actual
# kotlin-stdlib/-reflect/-test binaries it documents from a Maven repo. Left to
# its own devices it looks for "<kotlin-repo-root>/build/repo" at the checkout's
# own defaultSnapshotVersion - i.e. artifacts that only exist if you have built
# the entire kotlin repo locally first, and that are published nowhere public.
# Against a plain `git clone --depth 1` that resolves to nothing and the build
# fails before generating any docs. --kotlin-libs-version / --kotlin-libs-repo
# point it at already-published artifacts instead, which is hours of CI cheaper
# than building Kotlin just to document it.
#
# Only the final output path is written to stdout; every other message goes
# to stderr, so this composes as:
#   STDLIB_ALL_LIBS="$(build-stdlib-json-docs.sh <kotlin-repo-root>)"
#
# Usage:
#   build-stdlib-json-docs.sh [options] <kotlin-repo-root> [output-dir]
#
# Options:
#   --kotlin-libs-version V  Version of the kotlin-stdlib/-reflect/-test
#                            artifacts to document (Gradle -PdeployVersion).
#                            Should match <kotlin-repo-root>'s checked-out ref.
#                            Default: unset, i.e. the checkout's own
#                            defaultSnapshotVersion, which needs a local build
#                            of the kotlin repo to exist.
#   --kotlin-libs-repo URL   Maven repo to resolve them from (Gradle
#                            -PkotlinLibsRepo). Default: unset. kotlin_big
#                            already declares mavenCentral(), so a released
#                            --kotlin-libs-version needs no repo override; this
#                            is for a private or snapshot repo.
set -euo pipefail

log() { echo "$@" >&2; }

usage() { log "Usage: $0 [--kotlin-libs-version V] [--kotlin-libs-repo URL] <path-to-kotlin-repo-root> [output-dir]"; }

KOTLIN_LIBS_VERSION=""
KOTLIN_LIBS_REPO=""
POSITIONAL=()
while [ $# -gt 0 ]; do
    case "$1" in
        --kotlin-libs-version) KOTLIN_LIBS_VERSION="$2"; shift 2 ;;
        --kotlin-libs-repo) KOTLIN_LIBS_REPO="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        --) shift; POSITIONAL+=("$@"); break ;;
        -*) log "error: unrecognized option '$1'"; usage; exit 1 ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done
set -- ${POSITIONAL[@]+"${POSITIONAL[@]}"}

if [ $# -lt 1 ]; then
    usage
    exit 1
fi

KOTLIN_ROOT="$(cd "$1" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/../../kdoc-to-json" && pwd)"
STDLIB_DOCS_DIR="$KOTLIN_ROOT/libraries/tools/kotlin-stdlib-docs"
OUTPUT_ROOT="$(mkdir -p "${2:-$SCRIPT_DIR/build-output}" && cd "${2:-$SCRIPT_DIR/build-output}" && pwd)"
JSON_OUTPUT_DIR="$OUTPUT_ROOT/json"

# Passed through only when set, so an unset value falls through to the
# build's own default rather than overriding it with an empty string.
ARTIFACT_ARGS=()
[ -n "$KOTLIN_LIBS_VERSION" ] && ARTIFACT_ARGS+=("-PdeployVersion=$KOTLIN_LIBS_VERSION")
[ -n "$KOTLIN_LIBS_REPO" ] && ARTIFACT_ARGS+=("-PkotlinLibsRepo=$KOTLIN_LIBS_REPO")

if [ ! -f "$KOTLIN_ROOT/gradle.properties" ]; then
    log "error: '$KOTLIN_ROOT' doesn't look like a kotlin repo checkout (missing gradle.properties)."
    exit 1
fi
if [ ! -f "$STDLIB_DOCS_DIR/settings.gradle.kts" ] || [ ! -x "$STDLIB_DOCS_DIR/gradlew" ]; then
    log "error: '$STDLIB_DOCS_DIR' doesn't look like a kotlin-stdlib-docs project (missing settings.gradle.kts or gradlew)."
    exit 1
fi
if [ ! -x "$PLUGIN_DIR/gradlew" ]; then
    log "error: kdoc-to-json plugin project not found at '$PLUGIN_DIR' (missing gradlew)."
    exit 1
fi

# The JSON-plugin-enabled build.gradle.kts we're about to install reads
# dokka_version as a plain Gradle project property (-Pdokka_version=...)
# rather than through this repo's own version catalog, so it has to be
# supplied explicitly - pulled from the same catalog entry the rest of the
# kotlin repo's Dokka usage is pinned to, so it never drifts out of sync.
# "|| true": under `set -e` a command substitution whose pipeline exits
# non-zero kills the script outright, so a catalog with no 'dokka =' entry
# (a kotlin ref that renamed the key) aborted "Step 4/5" with exit 1 and no
# output at all - the friendly message below was unreachable.
DOKKA_VERSION="$(grep -m1 '^dokka[[:space:]]*=' "$KOTLIN_ROOT/gradle/libs.versions.toml" | sed -E 's/^dokka[[:space:]]*=[[:space:]]*"([^"]*)".*/\1/' || true)"
if [ -z "$DOKKA_VERSION" ]; then
    log "error: couldn't find a 'dokka = \"...\"' entry in $KOTLIN_ROOT/gradle/libs.versions.toml"
    exit 1
fi

log "==> [1/2] Building and publishing a fresh copy of the kdoc-to-json plugin..."
# Sent to stderr (fd 2), not left on stdout - a caller doing
# STDLIB_ALL_LIBS="$(build-stdlib-json-docs.sh ...)" must only capture the
# final path this script echoes, not gradlew's own build console output.
( cd "$PLUGIN_DIR" && ./gradlew clean publishToMavenLocal ) >&2

log "==> Installing kdoc-to-json-enabled build.gradle.kts into $STDLIB_DOCS_DIR"
ORIGINAL_BUILD_GRADLE="$(mktemp)"
cp "$STDLIB_DOCS_DIR/build.gradle.kts" "$ORIGINAL_BUILD_GRADLE"
restore_build_gradle() {
    cp "$ORIGINAL_BUILD_GRADLE" "$STDLIB_DOCS_DIR/build.gradle.kts"
    rm -f "$ORIGINAL_BUILD_GRADLE"
}
# INT/TERM/HUP as well as EXIT: a bare EXIT trap doesn't run on an untrapped
# fatal signal, so Ctrl-C during the long Gradle build left the swapped-in
# build.gradle.kts sitting in the developer's kotlin clone and orphaned the
# mktemp copy - contradicting this script's promise above that the checkout is
# left exactly as it was found.
trap restore_build_gradle EXIT INT TERM HUP
cp "$SCRIPT_DIR/build.gradle.kts" "$STDLIB_DOCS_DIR/build.gradle.kts"

log "==> [2/2] Generating JSON documentation via kdoc-to-json (dokka $DOKKA_VERSION)..."
log "    stdlib artifacts: ${KOTLIN_LIBS_VERSION:-(checkout default: needs a local kotlin build)}" \
    "from ${KOTLIN_LIBS_REPO:-(mavenCentral + checkout default repo)}"
# --refresh-dependencies forces Gradle to re-resolve the just-published
# SNAPSHOT jar from mavenLocal() rather than serving a same-GAV copy it
# cached from an earlier run of this same script.
( cd "$STDLIB_DOCS_DIR" && ./gradlew dokkaGenerateModuleJson \
    "-PdocsBuildDir=$JSON_OUTPUT_DIR" \
    "-Pdokka_version=$DOKKA_VERSION" \
    ${ARTIFACT_ARGS[@]+"${ARTIFACT_ARGS[@]}"} \
    --refresh-dependencies ) >&2

ALL_LIBS_DIR="$JSON_OUTPUT_DIR/latest/all-libs"
if [ ! -d "$ALL_LIBS_DIR" ]; then
    log "error: expected output at '$ALL_LIBS_DIR' but it wasn't created."
    exit 1
fi

log "==> Done."
echo "$ALL_LIBS_DIR"
