#!/usr/bin/env bash
# Runs the "Build Java Docs (Local)" GitHub Actions workflow
# (.github/workflows/build-java-docs-local.yaml) locally via act
# (https://github.com/nektos/act).
#
# This drives the *local* workflow, not build-java-docs.yaml. The Drive
# workflow authenticates to Google Cloud with Workload Identity Federation,
# and WIF validates the OIDC token's issuer against GitHub's own token
# endpoint for a specific repo and run - act cannot mint a token GCP will
# accept, so that workflow can never get past its auth step locally no matter
# what secrets you supply. build-java-docs-local.yaml exists precisely to be
# runnable here: it reads its documentation.db from disk and writes its outputs
# back to disk, and is otherwise step-for-step identical to the Drive workflow
# (same build-jdk-json-docs -> flatten_templates -> sync_javadoc_json_to_db,
# same parity and decode verification).
#
# The Kotlin counterpart is run-build-kotlin-docs-with-act.sh; this mirrors it.
#
# Requires:
#   - act (https://github.com/nektos/act#installation) on PATH
#   - a running Docker daemon (act executes each step inside a container)
#
# CONTAINER MEMORY: documenting the whole JDK analyses ~4,800 source files in
# one pass, inside the job container, and that does NOT fit a small container
# VM. Measured on a colima VM with 7.7 GB, --modules unset:
#
#   (no flag, 24g ceiling)   exit 137 after 1m17  - kernel SIGKILL: the JVM
#                                                   grows until the VM is out
#   --dokka-worker-heap 6g   exit 137 after 2m26  - same
#   --dokka-worker-heap 5g   exit 137 after 2m33  - same
#   --dokka-worker-heap 4g   "Java heap space"    - the JVM hit its own cap;
#                                                   4g is too little for the job
#
# So no heap setting works at 7.7 GB: below ~5g the analysis genuinely needs
# more, and at ~5g and up the process no longer fits alongside the Gradle
# daemon. Raise the container VM instead. Measured working configuration:
#
#   colima start --memory 16     (docker reports 15.6 GB)
#   --dokka-worker-heap 8g       full JDK, all 60 modules, ~4 minutes:
#                                2m30 generating, 1m15 syncing
#
# Or use --modules to document a subset, which runs in about a minute on the
# smaller VM and is enough to exercise the whole pipeline.
#
# Those two failure modes are also how to tell whether --dokka-worker-heap took
# effect at all: "Java heap space" means the JVM hit the cap you set, exit 137
# means it was killed from outside. The build additionally logs a "Dokka worker
# heap" line whenever the flag is applied.
#
# Secrets: none are required. SLACK_WEBHOOK_URL is the only secret this
# workflow reads, and it is optional - the two "Notify Slack" steps print a
# skip notice and continue when it is unset. Export it if you want to see them
# actually fire ("build complete" additionally needs --live, since it is gated
# on dry_run being false). GitHub never exposes a stored secret's value through
# any API or CLI, so if you do want the real webhook you have to supply your
# own copy of the value.
#
# Inputs are host paths, bind-mounted into the job container at fixed
# locations and passed to the workflow as those in-container paths (a
# GitHub-hosted runner has no access to your disk, so the workflow only ever
# sees the mounted paths). Note this means the host paths must live somewhere
# your container runtime is allowed to share - under $HOME is safe for both
# colima and Docker Desktop; /tmp on macOS often is not.
#
# Usage:
#   ./run-build-java-docs-with-act.sh --db-path PATH [options] [-- <extra act args>]
#
# Options:
#   --db-path PATH            Host path to the input documentation.db (required).
#                             With --live this file is overwritten in place.
#   --output-dir PATH         Host directory for outputs - a run-numbered copy
#                             of the built database. Created if absent.
#                             (default: ./build-java-docs-output)
#   --live                    dry_run=false: write the rebuilt database back
#                             over --db-path when the run finishes. Also
#                             required for the "build complete" Slack
#                             notification to fire. Default is dry_run=true.
#   --java-version V          JDK whose lib/src.zip is documented, and which
#                             the Gradle builds run on (default: 17). Must be
#                             21 or lower: kdoc-to-json is pinned to Kotlin
#                             1.9.24, whose compiler cannot run on a newer JDK.
#   --modules LIST            Comma-separated JPMS modules to document instead
#                             of all of them, e.g. "java.sql,java.xml". Turns a
#                             multi-minute run into seconds, which is the way
#                             to smoke-test a change. Requires
#                             --no-verify-parity, since a subset cannot match
#                             the full reference docs.
#   --dokka-worker-heap SIZE  Max heap for Dokka's worker process, e.g. 6g.
#                             Only needed if a run OOMs (see CONTAINER MEMORY).
#   --no-verify-parity        verify_parity=false: skip the comparison against
#                             the reference javadoc in SourceDocs/JavaDocs.
#   --delete-missing          delete_missing=true: also delete j/html/api/ rows
#                             with no JSON counterpart (class-use/,
#                             package-use, the tree pages). About half the
#                             rows. Default is to leave them alone.
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
WORKFLOW="$REPO_ROOT/.github/workflows/build-java-docs-local.yaml"

# Where the host paths below get bind-mounted inside the job container, and
# therefore what the workflow itself is told its inputs are.
CONTAINER_DB_PATH="/mnt/act-inputs/documentation.db"
CONTAINER_OUTPUT_DIR="/mnt/act-output"

DB_PATH=""
OUTPUT_DIR="$REPO_ROOT/build-java-docs-output"
# Mirrors build-java-docs-local.yaml's own default. Restated here because act
# does not apply workflow_dispatch defaults (see the note above); passing the
# input unconditionally is what keeps a local run equivalent to a real one.
JAVA_VERSION="17"
MODULES=""
DOKKA_WORKER_HEAP=""
VERIFY_PARITY="true"
DELETE_MISSING="false"
DRY_RUN="true"

EXTRA_ACT_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --db-path) DB_PATH="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --live) DRY_RUN="false"; shift ;;
    --java-version) JAVA_VERSION="$2"; shift 2 ;;
    --modules) MODULES="$2"; shift 2 ;;
    --dokka-worker-heap) DOKKA_WORKER_HEAP="$2"; shift 2 ;;
    --no-verify-parity) VERIFY_PARITY="false"; shift ;;
    --delete-missing) DELETE_MISSING="true"; shift ;;
    --) shift; EXTRA_ACT_ARGS+=("$@"); break ;;
    *) echo "error: unrecognized argument '$1'" >&2; exit 1 ;;
  esac
done

if [ -z "$DB_PATH" ]; then
  echo "error: --db-path is required (host path to the documentation.db to build against)" >&2
  exit 1
fi
if [ ! -f "$DB_PATH" ]; then
  echo "error: --db-path '$DB_PATH' does not exist or is not a file" >&2
  exit 1
fi
DB_PATH="$(cd "$(dirname "$DB_PATH")" && pwd)/$(basename "$DB_PATH")"

if [ -n "$MODULES" ] && [ "$VERIFY_PARITY" = "true" ]; then
  echo "error: --modules documents only part of the JDK, so the parity check against the" >&2
  echo "error: full reference docs in SourceDocs/JavaDocs is guaranteed to fail. Pass" >&2
  echo "error: --no-verify-parity alongside it." >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

# One -v per input. Mounting the database individually (rather than its parent
# directory) keeps the container's view to exactly what the run needs, and lets
# --db-path and --output-dir live in unrelated places on the host.
#
# act takes --container-options as one string and splits it with shell-style
# quoting rules, so each mount spec is emitted double-quoted: an unquoted join
# would break the moment a host path contained a space.
CONTAINER_OPTIONS=""
add_mount() { CONTAINER_OPTIONS+=" -v \"$1:$2\""; }
add_mount "$DB_PATH" "$CONTAINER_DB_PATH"
add_mount "$OUTPUT_DIR" "$CONTAINER_OUTPUT_DIR"

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
echo "   db_path      $DB_PATH -> $CONTAINER_DB_PATH"
echo "   output_dir   $OUTPUT_DIR -> $CONTAINER_OUTPUT_DIR"
echo "   java_version=$JAVA_VERSION modules=${MODULES:-(all)} dry_run=$DRY_RUN"
echo "   verify_parity=$VERIFY_PARITY delete_missing=$DELETE_MISSING"
echo "   dokka_worker_heap=${DOKKA_WORKER_HEAP:-(build default)}"

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
  --input output_dir="$CONTAINER_OUTPUT_DIR" \
  --input java_version="$JAVA_VERSION" \
  --input modules="$MODULES" \
  --input dokka_worker_heap="$DOKKA_WORKER_HEAP" \
  --input verify_parity="$VERIFY_PARITY" \
  --input delete_missing="$DELETE_MISSING" \
  --input dry_run="$DRY_RUN" \
  ${SECRET_ARGS[@]+"${SECRET_ARGS[@]}"} \
  ${EXTRA_ACT_ARGS[@]+"${EXTRA_ACT_ARGS[@]}"}
