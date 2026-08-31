#!/usr/bin/env bash
# Runs the "Build Kotlin Docs (Local)" GitHub Actions workflow
# (.github/workflows/build-kotlin-docs-local.yaml) locally via act
# (https://github.com/nektos/act).
#
# This drives the *local* workflow, not build-kotlin-docs.yaml. The Drive
# workflow authenticates to Google Cloud with Workload Identity Federation,
# and WIF validates the OIDC token's issuer against GitHub's own token
# endpoint for a specific repo and run - act cannot mint a token GCP will
# accept, so that workflow can never get past its auth step locally no matter
# what secrets you supply. build-kotlin-docs-local.yaml exists precisely to
# be runnable here: it reads its documentation.db / webHelpImages.zip from
# disk and writes its outputs back to disk, and is otherwise step-for-step
# identical to the Drive workflow (same find_missing_assets -> populate_db ->
# insert_optimized_media -> build-stdlib-json-docs -> sync_kdoc_json_to_db,
# same ADFA-4737 blacklist, same verification).
#
# Requires:
#   - act (https://github.com/nektos/act#installation) on PATH
#   - a running Docker daemon (act executes each step inside a container)
#
# Secrets: none are required. SLACK_WEBHOOK_URL is the only secret this
# workflow reads, and it is optional - the two "Notify Slack" steps print a
# skip notice and continue when it is unset. Export it if you want to see
# them actually fire ("build complete" additionally needs --live, since it is
# gated on dry_run being false). GitHub never exposes a stored secret's value
# through any API or CLI, so if you do want the real webhook you have to
# supply your own copy of the value.
#
# Inputs are host paths, bind-mounted into the job container at fixed
# locations and passed to the workflow as those in-container paths (a
# GitHub-hosted runner has no access to your disk, so the workflow only ever
# sees the mounted paths). Note this means the host paths must live somewhere
# your container runtime is allowed to share - under $HOME is safe for both
# colima and Docker Desktop; /tmp on macOS often is not.
#
# Usage:
#   ./run-build-kotlin-docs-with-act.sh --db-path PATH [options] [-- <extra act args>]
#
# Options:
#   --db-path PATH             Host path to the input documentation.db (required).
#                              With --live this file is overwritten in place.
#   --images-zip-path PATH     Host path to Writerside's webHelpImages.zip.
#                              Required unless --skip-website-docs.
#   --output-dir PATH          Host directory for outputs - the missing-assets
#                              report and a run-numbered copy of the built
#                              database. Created if absent.
#                              (default: ./build-kotlin-docs-output)
#   --live                     dry_run=false: write the rebuilt database back
#                              over --db-path when the run finishes. Also
#                              required for the "build complete" Slack
#                              notification to fire. Default is dry_run=true.
#   --skip-website-docs        skip_website_docs=true (default: false)
#   --skip-stdlib-docs         skip_stdlib_docs=true (default: false). Skips
#                              cloning JetBrains/kotlin and the Dokka JSON
#                              build - by far the slowest part of a run, and
#                              the half you don't need when iterating on the
#                              kotlin-web-site content.
#   --kotlin-web-site-ref REF  kotlin_web_site_ref input (default: '')
#   --kotlin-ref REF           kotlin_ref input (default: '')
#   --kotlin-libs-version V    Version of the published kotlin-stdlib/-reflect/
#                              -test artifacts to document. Defaults to the
#                              workflow's own default; pass '' to fall back to
#                              the kotlin checkout's snapshot version, which
#                              only resolves if you built the kotlin repo.
#   --kotlin-libs-repo URL     Maven repo to resolve them from (default: '',
#                              i.e. mavenCentral, which is enough for a
#                              released --kotlin-libs-version).
#
# Every workflow input is passed explicitly on every run, including the ones
# whose YAML "default:" would cover them. act does not apply
# workflow_dispatch input defaults - an input you don't pass arrives empty -
# and for dry_run that inverts the intended behaviour: "${{ !inputs.dry_run }}"
# on an empty value is true, so the step that writes the database back over
# --db-path would run. Passing all of them keeps a local run's semantics
# identical to a real dispatch.
#
# On Apple Silicon act warns about container architecture; append
# `-- --container-architecture linux/arm64` if you want to silence it (the
# default works).
set -euo pipefail

if ! command -v act >/dev/null 2>&1; then
  echo "error: act is required - see https://github.com/nektos/act#installation" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW="$REPO_ROOT/.github/workflows/build-kotlin-docs-local.yaml"

# Where the host paths below get bind-mounted inside the job container, and
# therefore what the workflow itself is told its inputs are.
CONTAINER_DB_PATH="/mnt/act-inputs/documentation.db"
CONTAINER_IMAGES_ZIP_PATH="/mnt/act-inputs/webHelpImages.zip"
CONTAINER_OUTPUT_DIR="/mnt/act-output"

DB_PATH=""
IMAGES_ZIP_PATH=""
OUTPUT_DIR="$REPO_ROOT/build-kotlin-docs-output"
KOTLIN_WEB_SITE_REF=""
KOTLIN_REF=""
# Mirrors build-kotlin-docs-local.yaml's own default. Restated here because act
# does not apply workflow_dispatch defaults (see the note above); passing the
# input unconditionally is what keeps a local run equivalent to a real one.
KOTLIN_LIBS_VERSION="2.4.10"
KOTLIN_LIBS_REPO=""
SKIP_WEBSITE_DOCS="false"
SKIP_STDLIB_DOCS="false"
DRY_RUN="true"

EXTRA_ACT_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --db-path) DB_PATH="$2"; shift 2 ;;
    --images-zip-path) IMAGES_ZIP_PATH="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --live) DRY_RUN="false"; shift ;;
    --skip-website-docs) SKIP_WEBSITE_DOCS="true"; shift ;;
    --skip-stdlib-docs) SKIP_STDLIB_DOCS="true"; shift ;;
    --kotlin-web-site-ref) KOTLIN_WEB_SITE_REF="$2"; shift 2 ;;
    --kotlin-ref) KOTLIN_REF="$2"; shift 2 ;;
    --kotlin-libs-version) KOTLIN_LIBS_VERSION="$2"; shift 2 ;;
    --kotlin-libs-repo) KOTLIN_LIBS_REPO="$2"; shift 2 ;;
    --) shift; EXTRA_ACT_ARGS+=("$@"); break ;;
    *) echo "error: unrecognized argument '$1'" >&2; exit 1 ;;
  esac
done

if [ "$SKIP_WEBSITE_DOCS" = "true" ] && [ "$SKIP_STDLIB_DOCS" = "true" ]; then
  echo "error: --skip-website-docs and --skip-stdlib-docs together skip every step that" >&2
  echo "error: changes the database, leaving nothing for the run to do." >&2
  exit 1
fi

if [ -z "$DB_PATH" ]; then
  echo "error: --db-path is required (host path to the documentation.db to build against)" >&2
  exit 1
fi
if [ ! -f "$DB_PATH" ]; then
  echo "error: --db-path '$DB_PATH' does not exist or is not a file" >&2
  exit 1
fi
DB_PATH="$(cd "$(dirname "$DB_PATH")" && pwd)/$(basename "$DB_PATH")"

if [ "$SKIP_WEBSITE_DOCS" != "true" ]; then
  if [ -z "$IMAGES_ZIP_PATH" ]; then
    echo "error: --images-zip-path is required unless --skip-website-docs is passed." >&2
    echo "error: Writerside's webHelpImages.zip is only produced by IntelliJ IDEA's" >&2
    echo "error: Writerside plugin - there is no headless way to generate it. See the" >&2
    echo "error: KNOWN LIMITATION note at the top of $WORKFLOW." >&2
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

# One -v per input. Mounting the files individually (rather than their parent
# directories) keeps the container's view to exactly what the run needs, and
# lets --db-path and --images-zip-path live in unrelated places on the host.
#
# act takes --container-options as one string and splits it with shell-style
# quoting rules, so each mount spec is emitted double-quoted: an unquoted
# join would break the moment a host path contained a space.
CONTAINER_OPTIONS=""
add_mount() { CONTAINER_OPTIONS+=" -v \"$1:$2\""; }
add_mount "$DB_PATH" "$CONTAINER_DB_PATH"
add_mount "$OUTPUT_DIR" "$CONTAINER_OUTPUT_DIR"
WORKFLOW_IMAGES_ZIP_PATH=""
if [ "$SKIP_WEBSITE_DOCS" != "true" ]; then
  add_mount "$IMAGES_ZIP_PATH" "$CONTAINER_IMAGES_ZIP_PATH"
  WORKFLOW_IMAGES_ZIP_PATH="$CONTAINER_IMAGES_ZIP_PATH"
fi

if [ "$DRY_RUN" = "true" ]; then
  echo "note: dry_run=true - '$DB_PATH' will NOT be modified; the built database is" >&2
  echo "note: written to '$OUTPUT_DIR' only. The 'build started' Slack notification" >&2
  echo "note: still fires (if SLACK_WEBHOOK_URL is set) but 'build complete' is gated" >&2
  echo "note: on dry_run=false. Pass --live to write back and see it." >&2
else
  echo "WARNING: --live - '$DB_PATH' will be OVERWRITTEN in place when the run finishes." >&2
fi

# The workflow reads SLACK_WEBHOOK_URL and tolerates it being unset, so pass
# it through when it's in the environment and stay silent when it isn't.
#
# SECRET_ARGS and EXTRA_ACT_ARGS are expanded below as
# ${arr[@]+"${arr[@]}"} rather than plain "${arr[@]}": macOS still ships bash
# 3.2, where `set -u` treats an empty array's "${arr[@]}" as an unbound
# variable and aborts. Both arrays are empty on a normal run.
SECRET_ARGS=()
if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
  SECRETS_FILE="$(mktemp)"
  trap 'rm -f "$SECRETS_FILE"' EXIT
  printf 'SLACK_WEBHOOK_URL=%s\n' "$SLACK_WEBHOOK_URL" > "$SECRETS_FILE"
  SECRET_ARGS=(--secret-file "$SECRETS_FILE")
fi

echo "== Running $WORKFLOW via act =="
echo "   db_path          $DB_PATH -> $CONTAINER_DB_PATH"
echo "   images_zip_path  ${IMAGES_ZIP_PATH:-(skipped)}${IMAGES_ZIP_PATH:+ -> $CONTAINER_IMAGES_ZIP_PATH}"
echo "   output_dir       $OUTPUT_DIR -> $CONTAINER_OUTPUT_DIR"
echo "   dry_run=$DRY_RUN skip_website_docs=$SKIP_WEBSITE_DOCS skip_stdlib_docs=$SKIP_STDLIB_DOCS"
if [ "$SKIP_STDLIB_DOCS" != "true" ]; then
  echo "   stdlib artifacts  ${KOTLIN_LIBS_VERSION:-(kotlin checkout default)} from ${KOTLIN_LIBS_REPO:-(mavenCentral)}"
fi

# --container-daemon-socket - : act otherwise bind-mounts the host's Docker
# socket into the job container so steps can run Docker themselves. Nothing in
# this workflow does, and the mount outright fails on runtimes whose socket
# isn't a plain bind-mountable file - under colima it aborts the run with
# "error while creating mount source path ...: operation not supported".
act workflow_dispatch \
  -W "$WORKFLOW" \
  -P ubuntu-latest=catthehacker/ubuntu:act-latest \
  --container-daemon-socket - \
  --container-options "$CONTAINER_OPTIONS" \
  --input db_path="$CONTAINER_DB_PATH" \
  --input images_zip_path="$WORKFLOW_IMAGES_ZIP_PATH" \
  --input output_dir="$CONTAINER_OUTPUT_DIR" \
  --input kotlin_web_site_ref="$KOTLIN_WEB_SITE_REF" \
  --input kotlin_ref="$KOTLIN_REF" \
  --input kotlin_libs_version="$KOTLIN_LIBS_VERSION" \
  --input kotlin_libs_repo="$KOTLIN_LIBS_REPO" \
  --input skip_website_docs="$SKIP_WEBSITE_DOCS" \
  --input skip_stdlib_docs="$SKIP_STDLIB_DOCS" \
  --input dry_run="$DRY_RUN" \
  ${SECRET_ARGS[@]+"${SECRET_ARGS[@]}"} \
  ${EXTRA_ACT_ARGS[@]+"${EXTRA_ACT_ARGS[@]}"}
