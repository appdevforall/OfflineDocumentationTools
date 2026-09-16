#!/usr/bin/env bash
# Runs the "Build Android Docs (Local)" GitHub Actions workflow
# (.github/workflows/build-android-docs-local.yaml) locally via act
# (https://github.com/nektos/act).
#
# This drives the *local* workflow, not build-android-docs.yaml. The Drive
# workflow authenticates to Google Cloud with Workload Identity Federation, and
# WIF validates the OIDC token's issuer against GitHub's own token endpoint for
# a specific repo and run - act cannot mint a token GCP will accept, so that
# workflow can never get past its auth step locally no matter what secrets you
# supply. build-android-docs-local.yaml exists precisely to be runnable here: it
# reads its documentation.db from disk and writes its outputs back to disk, and
# is otherwise step-for-step identical to the Drive workflow (same
# android_docs_to_json -> load_android_json_db -> verify_android_json_db ->
# remint_dictionary -> verify_remint_dictionary, same verification).
#
# The Java counterpart is run-build-java-docs-with-act.sh; this mirrors it.
#
# Requires:
#   - act (https://github.com/nektos/act#installation) on PATH
#   - a running Docker daemon (act executes each step inside a container)
#
# CONTAINER SIZE: unlike the Java pipeline, nothing here is memory-hungry - the
# conversion is one HTML parse per page across worker processes, and the largest
# page in the corpus parses in well under a second. A default container VM is
# fine. What it does want is disk and time:
#
#   ~730 MB  the scraped HTML, already in the checkout act copies in
#   ~350 MB  the JSON tree the conversion writes
#   ~2x      the database, since the load builds a new one beside the input
#
# so allow roughly 2 GB of free space in the container beyond the checkout.
#
# TIMING, measured against the real corpus and database on a 10-core machine:
#
#   convert           ~1 min     12,106 pages -> 12,906 JSON documents
#   load              ~1.5 min   rows, templates, stylesheet
#   verify (40 pages) ~1 min     one JVM per sampled page
#   re-mint           ~5.5 min   recompresses EVERY row in the database, not
#                                just the Android ones, at Brotli q11
#   verify re-mint    ~2 min     decompresses both databases and compares
#
# About 12 minutes all told. --only cuts the first three to seconds and is the
# way to smoke-test a change; --skip-remint drops the last two.
#
# Secrets: none are required. SLACK_WEBHOOK_URL is the only secret this workflow
# reads, and it is optional - the two "Notify Slack" steps print a skip notice
# and continue when it is unset. Export it if you want to see them actually fire
# (both need --live: the baton stands for the lock on db_path, so neither half
# of the pair runs on a dry run, which rewrites nothing). GitHub never exposes a
# stored secret's value through any API or CLI, so if you do want the real
# webhook you have to supply your own copy.
#
# Inputs are host paths, bind-mounted into the job container at fixed locations
# and passed to the workflow as those in-container paths (a GitHub-hosted runner
# has no access to your disk, so the workflow only ever sees the mounted paths).
# Note this means the host paths must live somewhere your container runtime is
# allowed to share - under $HOME is safe for both colima and Docker Desktop;
# /tmp on macOS often is not.
#
# Usage:
#   ./run-build-android-docs-with-act.sh --db-path PATH [options] [-- <extra act args>]
#
# Options:
#   --db-path PATH       Host path to the input documentation.db (required).
#                        With --live this file is overwritten in place.
#   --output-dir PATH    Host directory for outputs - a run-numbered copy of the
#                        built database. Created if absent.
#                        (default: ./build-android-docs-output)
#   --live               dry_run=false: write the rebuilt database back over
#                        --db-path when the run finishes. Also required for
#                        either Slack notification to fire. Default is
#                        dry_run=true.
#   --only SUBSTRING     Convert only pages whose path contains SUBSTRING, e.g.
#                        "android/app/". Turns a multi-minute run into seconds.
#                        Requires --no-verify-complete, since the rest of the
#                        corpus is deliberately left as it was.
#   --sample-size N      How many stored pages to render back out of the built
#                        database (default: 40). Each one starts a JVM.
#   --no-verify-complete verify_complete=false: do not fail when Android rows are
#                        still serving raw HTML. Only for an --only run.
#   --skip-remint        skip_remint=true: leave the shared Brotli dictionary
#                        alone. Saves most of the run time. Right for a
#                        structural check, and for a re-run against a database
#                        already minted for this corpus - re-minting is not
#                        cumulative, and a second pass over an already-minted
#                        database measured -0.7%. Otherwise it is worth about
#                        16% on a database coming from HTML.
#
# Every workflow input is passed explicitly on every run, including the ones
# whose YAML "default:" would cover them. act does not apply workflow_dispatch
# input defaults - an input you don't pass arrives empty - and for dry_run that
# inverts the intended behaviour: "${{ !inputs.dry_run }}" on an empty value is
# true, so the step that writes the database back over --db-path would run.
# Passing all of them keeps a local run's semantics identical to a real dispatch.
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
WORKFLOW="$REPO_ROOT/.github/workflows/build-android-docs-local.yaml"

# Where the host paths below get bind-mounted inside the job container, and
# therefore what the workflow itself is told its inputs are.
CONTAINER_DB_PATH="/mnt/act-inputs/documentation.db"
CONTAINER_OUTPUT_DIR="/mnt/act-output"

DB_PATH=""
OUTPUT_DIR="$REPO_ROOT/build-android-docs-output"
# Mirrors build-android-docs-local.yaml's own defaults. Restated here because
# act does not apply workflow_dispatch defaults (see the note above); passing
# every input unconditionally is what keeps a local run equivalent to a real one.
ONLY=""
SAMPLE_SIZE="40"
VERIFY_COMPLETE="true"
SKIP_REMINT="false"
DRY_RUN="true"

EXTRA_ACT_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --db-path) DB_PATH="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --live) DRY_RUN="false"; shift ;;
    --only) ONLY="$2"; shift 2 ;;
    --sample-size) SAMPLE_SIZE="$2"; shift 2 ;;
    --no-verify-complete) VERIFY_COMPLETE="false"; shift ;;
    --skip-remint) SKIP_REMINT="true"; shift ;;
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

if [ -n "$ONLY" ] && [ "$VERIFY_COMPLETE" = "true" ]; then
  echo "error: --only converts part of the corpus, so the rest of the Android rows stay as" >&2
  echo "error: they were and the completeness check is guaranteed to fail. Pass" >&2
  echo "error: --no-verify-complete alongside it." >&2
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
  echo "note: written to '$OUTPUT_DIR' only. Neither Slack notification fires: the" >&2
  echo "note: baton stands for the lock on db_path, and a dry run never takes it." >&2
  echo "note: Pass --live to write back and see both." >&2
else
  echo "WARNING: --live - '$DB_PATH' will be OVERWRITTEN in place when the run finishes." >&2
fi

if [ "$SKIP_REMINT" = "true" ]; then
  echo "note: --skip-remint - the shared Brotli dictionary is left as it is. On a database" >&2
  echo "note: coming from HTML that costs about 16% on the Android pages; on one already" >&2
  echo "note: minted for this corpus it costs nothing, and skipping saves ~5.5 minutes." >&2
fi

# The workflow reads SLACK_WEBHOOK_URL and tolerates it being unset, so pass it
# through when it's in the environment and stay silent when it isn't.
#
# SECRET_ARGS and EXTRA_ACT_ARGS are expanded below as ${arr[@]+"${arr[@]}"}
# rather than plain "${arr[@]}": macOS still ships bash 3.2, where `set -u`
# treats an empty array's "${arr[@]}" as an unbound variable and aborts. Both
# arrays are empty on a normal run.
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
echo "   only=${ONLY:-(whole corpus)} sample_size=$SAMPLE_SIZE dry_run=$DRY_RUN"
echo "   verify_complete=$VERIFY_COMPLETE skip_remint=$SKIP_REMINT"

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
  --input only="$ONLY" \
  --input sample_size="$SAMPLE_SIZE" \
  --input verify_complete="$VERIFY_COMPLETE" \
  --input skip_remint="$SKIP_REMINT" \
  --input dry_run="$DRY_RUN" \
  ${SECRET_ARGS[@]+"${SECRET_ARGS[@]}"} \
  ${EXTRA_ACT_ARGS[@]+"${EXTRA_ACT_ARGS[@]}"}
