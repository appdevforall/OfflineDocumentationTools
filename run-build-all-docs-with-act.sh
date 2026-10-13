#!/usr/bin/env bash
# Runs the "Build All Docs (Local)" GitHub Actions workflow
# (.github/workflows/build-all-docs-local.yaml) locally via act
# (https://github.com/nektos/act).
#
# This drives the *local* workflow, not build-all-docs.yaml. The Drive workflow authenticates to
# Google Cloud with Workload Identity Federation, and WIF validates the OIDC token's issuer
# against GitHub's own token endpoint for a specific repo and run - act cannot mint a token GCP
# will accept, so that workflow can never get past its auth step locally. build-all-docs-local.yaml
# exists precisely to be runnable here: it reads its documentation.db / webHelpImages.zip from disk
# and writes the new database back to disk, and is otherwise step-for-step identical to the Drive
# workflow (Kotlin website -> kotlin-stdlib -> Java -> Android/AndroidX -> dictionary re-mint ->
# whole-database verification).
#
# It is the four single-set runners in one: run-build-kotlin-docs-with-act.sh,
# run-build-java-docs-with-act.sh and run-build-android-docs-with-act.sh. Their notes on
# container memory, disk and timing add up here - in particular the Java step's full-JDK Dokka pass
# does not fit a 7.7 GB container VM (see run-build-java-docs-with-act.sh: colima --memory 16 with
# --dokka-worker-heap 8g works), and the Android step wants ~2 GB of free disk. To exercise every
# step quickly on a small VM:
#
#   ./run-build-all-docs-with-act.sh \
#       --db-path ~/docs/documentation.db --images-zip-path ~/docs/webHelpImages.zip \
#       --java-modules java.sql,java.transaction.xa --no-verify-java-parity \
#       --android-only android/app/
#
# Requires:
#   - act (https://github.com/nektos/act#installation) on PATH
#   - a running Docker daemon (act executes each step inside a container)
#
# Secrets: none are required. SLACK_WEBHOOK_URL is the only secret the workflow reads, and it is
# optional - the "Notify Slack" steps print a skip notice when it is unset. Export it to see them
# fire; supply your own copy, since GitHub never exposes a stored secret's value.
#
# Inputs are host paths, bind-mounted into the job container at fixed locations and passed to the
# workflow as those in-container paths. The host paths must live somewhere your container runtime
# may share - under $HOME is safe for both colima and Docker Desktop; /tmp on macOS often is not.
#
# --db-path and --images-zip-path are mounted :ro. The workflow never writes db_path, and refuses
# an output_db_path that resolves to it; the read-only mount is the part of that guarantee that
# does not depend on the workflow - which is why --output-db-path defaults into --output-dir.
#
# Usage:
#   ./run-build-all-docs-with-act.sh --db-path PATH [options] [-- <extra act args>]
#
# Options:
#   --db-path PATH              Host path to the input documentation.db (required). Never
#                               written to (mounted read-only).
#   --images-zip-path PATH      Host path to Writerside's webHelpImages.zip. Required unless
#                               --skip-kotlin-website.
#   --output-dir PATH           Host directory for outputs: a run-numbered copy of the new
#                               database, the missing-assets report. Created if absent.
#                               (default: ./build-all-docs-output)
#   --output-db-path PATH       Host path to save the new database to; must be under
#                               --output-dir. Default: <output-dir>/documentation.db.
#   --live                      dry_run=false: actually write --output-db-path. Default is
#                               dry_run=true, which still writes the run-numbered copy.
#   --skip-kotlin-website       skip_kotlin_website=true
#   --skip-kotlin-stdlib        skip_kotlin_stdlib=true (skips cloning JetBrains/kotlin and its
#                               Dokka build - the slowest part of a run)
#   --skip-java                 skip_java=true
#   --skip-android              skip_android=true
#   --skip-remint               skip_remint=true (structural checks only)
#   --kotlin-web-site-ref REF   kotlin_web_site_ref (default: '')
#   --kotlin-ref REF            kotlin_ref (default: '')
#   --kotlin-libs-version V     kotlin_libs_version (default: the workflow's, 2.4.10)
#   --kotlin-libs-repo URL      kotlin_libs_repo (default: '', i.e. mavenCentral)
#   --java-version V            java_version (default: 17; must be 17 to 21)
#   --java-modules LIST         java_modules, e.g. "java.sql,java.xml". Requires
#                               --no-verify-java-parity.
#   --dokka-worker-heap SIZE    dokka_worker_heap, e.g. 8g
#   --no-verify-java-parity     verify_java_parity=false
#   --java-delete-missing       java_delete_missing=true
#   --android-only SUBSTRING    android_only, e.g. "android/app/"
#   --android-sample-size N     android_sample_size (default: 40)
#   --render-sample-size N      render_sample_size (default: 20)
#
# Every workflow input is passed explicitly on every run, including the ones whose YAML
# "default:" would cover them: act does not apply workflow_dispatch input defaults, and for
# dry_run an empty value inverts the intended behaviour ("${{ !inputs.dry_run }}" is true).
#
# On Apple Silicon act warns about container architecture; append
# `-- --container-architecture linux/arm64` if you want to silence it (the default works).
set -euo pipefail

# $2 is read unguarded otherwise, so a trailing flag dies with bash's own
# "$2: unbound variable" under `set -u` rather than a usable message.
need_value() { [ $# -ge 2 ] || { echo "error: $1 needs a value" >&2; exit 1; }; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW="$REPO_ROOT/.github/workflows/build-all-docs-local.yaml"

# Where the host paths get bind-mounted inside the job container, and therefore what the workflow
# itself is told its inputs are.
CONTAINER_DB_PATH="/mnt/act-inputs/documentation.db"
CONTAINER_IMAGES_ZIP_PATH="/mnt/act-inputs/webHelpImages.zip"
CONTAINER_OUTPUT_DIR="/mnt/act-output"

DB_PATH=""
IMAGES_ZIP_PATH=""
OUTPUT_DIR="$REPO_ROOT/build-all-docs-output"
OUTPUT_DB_PATH=""
DRY_RUN="true"
SKIP_KOTLIN_WEBSITE="false"
SKIP_KOTLIN_STDLIB="false"
SKIP_JAVA="false"
SKIP_ANDROID="false"
SKIP_REMINT="false"
# The rest mirror build-all-docs-local.yaml's own defaults, restated because act does not apply
# them (see above).
KOTLIN_WEB_SITE_REF=""
KOTLIN_REF=""
KOTLIN_LIBS_VERSION="2.4.10"
KOTLIN_LIBS_REPO=""
JAVA_VERSION="17"
JAVA_MODULES=""
DOKKA_WORKER_HEAP=""
VERIFY_JAVA_PARITY="true"
JAVA_DELETE_MISSING="false"
ANDROID_ONLY=""
ANDROID_SAMPLE_SIZE="40"
RENDER_SAMPLE_SIZE="20"

EXTRA_ACT_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --db-path) need_value "$@"; DB_PATH="$2"; shift 2 ;;
    --images-zip-path) need_value "$@"; IMAGES_ZIP_PATH="$2"; shift 2 ;;
    --output-dir) need_value "$@"; OUTPUT_DIR="$2"; shift 2 ;;
    --output-db-path) need_value "$@"; OUTPUT_DB_PATH="$2"; shift 2 ;;
    --live) DRY_RUN="false"; shift ;;
    --skip-kotlin-website) SKIP_KOTLIN_WEBSITE="true"; shift ;;
    --skip-kotlin-stdlib) SKIP_KOTLIN_STDLIB="true"; shift ;;
    --skip-java) SKIP_JAVA="true"; shift ;;
    --skip-android) SKIP_ANDROID="true"; shift ;;
    --skip-remint) SKIP_REMINT="true"; shift ;;
    --kotlin-web-site-ref) need_value "$@"; KOTLIN_WEB_SITE_REF="$2"; shift 2 ;;
    --kotlin-ref) need_value "$@"; KOTLIN_REF="$2"; shift 2 ;;
    --kotlin-libs-version) need_value "$@"; KOTLIN_LIBS_VERSION="$2"; shift 2 ;;
    --kotlin-libs-repo) need_value "$@"; KOTLIN_LIBS_REPO="$2"; shift 2 ;;
    --java-version) need_value "$@"; JAVA_VERSION="$2"; shift 2 ;;
    --java-modules) need_value "$@"; JAVA_MODULES="$2"; shift 2 ;;
    --dokka-worker-heap) need_value "$@"; DOKKA_WORKER_HEAP="$2"; shift 2 ;;
    --no-verify-java-parity) VERIFY_JAVA_PARITY="false"; shift ;;
    --java-delete-missing) JAVA_DELETE_MISSING="true"; shift ;;
    --android-only) need_value "$@"; ANDROID_ONLY="$2"; shift 2 ;;
    --android-sample-size) need_value "$@"; ANDROID_SAMPLE_SIZE="$2"; shift 2 ;;
    --render-sample-size) need_value "$@"; RENDER_SAMPLE_SIZE="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    --) shift; EXTRA_ACT_ARGS+=("$@"); break ;;
    *) echo "error: unrecognized argument '$1'" >&2; exit 1 ;;
  esac
done

if [ "$SKIP_KOTLIN_WEBSITE" = "true" ] && [ "$SKIP_KOTLIN_STDLIB" = "true" ] \
   && [ "$SKIP_JAVA" = "true" ] && [ "$SKIP_ANDROID" = "true" ]; then
  echo "error: every documentation set is skipped - nothing for the run to do." >&2
  exit 1
fi
if [ -n "$JAVA_MODULES" ] && [ "$VERIFY_JAVA_PARITY" = "true" ] && [ "$SKIP_JAVA" != "true" ]; then
  echo "error: --java-modules documents only part of the JDK, so the parity check against the" >&2
  echo "error: full reference docs is guaranteed to fail. Pass --no-verify-java-parity too." >&2
  exit 1
fi

if [ -z "$DB_PATH" ]; then
  echo "error: --db-path is required (host path to the documentation.db to build from)" >&2
  exit 1
fi
if [ ! -f "$DB_PATH" ]; then
  echo "error: --db-path '$DB_PATH' does not exist or is not a file" >&2
  exit 1
fi
DB_PATH="$(cd "$(dirname "$DB_PATH")" && pwd)/$(basename "$DB_PATH")"

if [ "$SKIP_KOTLIN_WEBSITE" != "true" ]; then
  if [ -z "$IMAGES_ZIP_PATH" ]; then
    echo "error: --images-zip-path is required unless --skip-kotlin-website is passed." >&2
    echo "error: Writerside's webHelpImages.zip is only produced by IntelliJ IDEA's Writerside" >&2
    echo "error: plugin - see the KNOWN LIMITATION note in build-all-docs.yaml." >&2
    exit 1
  fi
  if [ ! -f "$IMAGES_ZIP_PATH" ]; then
    echo "error: --images-zip-path '$IMAGES_ZIP_PATH' does not exist or is not a file" >&2
    exit 1
  fi
  IMAGES_ZIP_PATH="$(cd "$(dirname "$IMAGES_ZIP_PATH")" && pwd)/$(basename "$IMAGES_ZIP_PATH")"
fi

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

# --output-db-path has to sit under --output-dir: that directory's mount is how anything the run
# writes reaches the host, and a file written anywhere else would be thrown away with the
# container. It is reached through that mount rather than one of its own because it generally
# names a file that does not exist yet, and Docker creates a *directory* at a missing bind source.
if [ -z "$OUTPUT_DB_PATH" ]; then
  OUTPUT_DB_PATH="$OUTPUT_DIR/documentation.db"
  CONTAINER_OUTPUT_DB_PATH="$CONTAINER_OUTPUT_DIR/documentation.db"
else
  OUTPUT_DB_DIR="$(cd "$(dirname "$OUTPUT_DB_PATH")" 2>/dev/null && pwd)" || {
    echo "error: --output-db-path '$OUTPUT_DB_PATH' is in a directory that does not exist" >&2
    exit 1
  }
  OUTPUT_DB_PATH="$OUTPUT_DB_DIR/$(basename "$OUTPUT_DB_PATH")"
  case "$OUTPUT_DB_PATH" in
    "$OUTPUT_DIR"/*)
      CONTAINER_OUTPUT_DB_PATH="$CONTAINER_OUTPUT_DIR/${OUTPUT_DB_PATH#"$OUTPUT_DIR"/}"
      ;;
    *)
      echo "error: --output-db-path '$OUTPUT_DB_PATH' is outside --output-dir '$OUTPUT_DIR'." >&2
      echo "error: Only --output-dir is mounted for writing; put it under there." >&2
      exit 1
      ;;
  esac
fi
if [ "$OUTPUT_DB_PATH" = "$DB_PATH" ]; then
  echo "error: --output-db-path is the same file as --db-path. This pipeline produces a new" >&2
  echo "error: database and keeps its input intact; pick another path." >&2
  exit 1
fi

# One -v per input. act takes --container-options as one string and splits it with shell-style
# quoting, so each mount spec is double-quoted: an unquoted join breaks on a path with a space.
CONTAINER_OPTIONS=""
add_mount() { CONTAINER_OPTIONS+=" -v \"$1:$2${3:-}\""; }
add_mount "$DB_PATH" "$CONTAINER_DB_PATH" ":ro"
add_mount "$OUTPUT_DIR" "$CONTAINER_OUTPUT_DIR"
WORKFLOW_IMAGES_ZIP_PATH=""
if [ "$SKIP_KOTLIN_WEBSITE" != "true" ]; then
  add_mount "$IMAGES_ZIP_PATH" "$CONTAINER_IMAGES_ZIP_PATH" ":ro"
  WORKFLOW_IMAGES_ZIP_PATH="$CONTAINER_IMAGES_ZIP_PATH"
fi

# Last, after every argument check, so a machine without act still gets told what is wrong with
# its arguments first (the lesson run-build-kotlin-docs-with-act.sh records).
if ! command -v act >/dev/null 2>&1; then
  echo "error: act is required - see https://github.com/nektos/act#installation" >&2
  exit 1
fi

if [ "$DRY_RUN" = "true" ]; then
  echo "note: dry_run=true - '$OUTPUT_DB_PATH' will NOT be written; the run-numbered copy" >&2
  echo "note: still lands in '$OUTPUT_DIR'. Pass --live to write it." >&2
else
  echo "note: --live - the new database will be saved to '$OUTPUT_DB_PATH'." >&2
  if [ -e "$OUTPUT_DB_PATH" ]; then
    echo "WARNING: '$OUTPUT_DB_PATH' already exists and will be overwritten." >&2
  fi
fi

# Expanded as ${arr[@]+"${arr[@]}"}: macOS's bash 3.2 treats an empty array as unbound under -u.
SECRET_ARGS=()
if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
  SECRETS_FILE="$(mktemp)"
  trap 'rm -f "$SECRETS_FILE"' EXIT
  printf 'SLACK_WEBHOOK_URL=%s\n' "$SLACK_WEBHOOK_URL" > "$SECRETS_FILE"
  SECRET_ARGS=(--secret-file "$SECRETS_FILE")
fi

echo "== Running $WORKFLOW via act =="
echo "   db_path          $DB_PATH -> $CONTAINER_DB_PATH (read-only)"
echo "   images_zip_path  ${IMAGES_ZIP_PATH:-(skipped)}${WORKFLOW_IMAGES_ZIP_PATH:+ -> $CONTAINER_IMAGES_ZIP_PATH (read-only)}"
echo "   output_dir       $OUTPUT_DIR -> $CONTAINER_OUTPUT_DIR"
echo "   output_db_path   $OUTPUT_DB_PATH -> $CONTAINER_OUTPUT_DB_PATH"
echo "   skip: kotlin_website=$SKIP_KOTLIN_WEBSITE kotlin_stdlib=$SKIP_KOTLIN_STDLIB java=$SKIP_JAVA android=$SKIP_ANDROID remint=$SKIP_REMINT"
echo "   java_version=$JAVA_VERSION java_modules=${JAVA_MODULES:-(all)} android_only=${ANDROID_ONLY:-(all)} dry_run=$DRY_RUN"

# --container-daemon-socket - : nothing here runs Docker itself, and act's default socket mount
# fails outright under colima (see run-build-kotlin-docs-with-act.sh).
act workflow_dispatch \
  -W "$WORKFLOW" \
  -P ubuntu-latest=catthehacker/ubuntu:act-latest \
  --container-daemon-socket - \
  --container-options "$CONTAINER_OPTIONS" \
  --input db_path="$CONTAINER_DB_PATH" \
  --input images_zip_path="$WORKFLOW_IMAGES_ZIP_PATH" \
  --input output_dir="$CONTAINER_OUTPUT_DIR" \
  --input output_db_path="$CONTAINER_OUTPUT_DB_PATH" \
  --input kotlin_web_site_ref="$KOTLIN_WEB_SITE_REF" \
  --input kotlin_ref="$KOTLIN_REF" \
  --input kotlin_libs_version="$KOTLIN_LIBS_VERSION" \
  --input kotlin_libs_repo="$KOTLIN_LIBS_REPO" \
  --input java_version="$JAVA_VERSION" \
  --input java_modules="$JAVA_MODULES" \
  --input dokka_worker_heap="$DOKKA_WORKER_HEAP" \
  --input verify_java_parity="$VERIFY_JAVA_PARITY" \
  --input java_delete_missing="$JAVA_DELETE_MISSING" \
  --input android_only="$ANDROID_ONLY" \
  --input android_sample_size="$ANDROID_SAMPLE_SIZE" \
  --input render_sample_size="$RENDER_SAMPLE_SIZE" \
  --input skip_kotlin_website="$SKIP_KOTLIN_WEBSITE" \
  --input skip_kotlin_stdlib="$SKIP_KOTLIN_STDLIB" \
  --input skip_java="$SKIP_JAVA" \
  --input skip_android="$SKIP_ANDROID" \
  --input skip_remint="$SKIP_REMINT" \
  --input dry_run="$DRY_RUN" \
  ${SECRET_ARGS[@]+"${SECRET_ARGS[@]}"} \
  ${EXTRA_ACT_ARGS[@]+"${EXTRA_ACT_ARGS[@]}"}
