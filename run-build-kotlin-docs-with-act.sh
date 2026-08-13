#!/usr/bin/env bash
# Runs the "Build Kotlin Docs" GitHub Actions workflow
# (.github/workflows/build-kotlin-docs.yaml) locally via act
# (https://github.com/nektos/act), for testing/inspecting the workflow
# without triggering a real run on GitHub.
#
# Requires:
#   - act (https://github.com/nektos/act#installation) on PATH
#   - Docker running (act executes each job step inside a container)
#
# GCP_WIF_PROVIDER, GCP_WIF_SERVICE_ACCOUNT, GOOGLE_DRIVE_FILE_ID,
# SLACK_WEBHOOK_URL and GOOGLE_DRIVE_IMAGES_ZIP_FILE_ID are secrets already
# configured on the real appdevforall/OfflineDocumentationTools repo for
# this workflow (confirmed via `gh secret list`) - this script uses those
# exact names so a secrets file built from your own local values lines up
# with what the workflow actually reads. GitHub never exposes a secret's
# *value* through any API/CLI once set (by design - only names are ever
# readable back), so there is no way for this script to fetch them itself;
# you still need to export your own equivalent values before running it.
#
# SLACK_WEBHOOK_URL specifically drives the workflow's two "Notify Slack"
# steps: "build started" runs unconditionally (once the secret is set) as
# soon as the database download succeeds, but "build complete" only runs
# `if: ${{ !inputs.dry_run }}` - so pass --live as well if you actually want
# to see the completion notification fire.
#
# Even with correct values, be aware that the "Authenticate to Google Cloud"
# step (google-github-actions/auth@v2, using GCP_WIF_PROVIDER/
# GCP_WIF_SERVICE_ACCOUNT) is very unlikely to succeed under local act: WIF
# validates the OIDC token's issuer against GitHub's own token endpoint for
# this specific repo/run, and act has no way to mint a token GCP will accept
# in its place. Treat a local run as a check of everything up to that step
# (job structure, find_missing_assets/populate_db/insert_optimized_media
# logic, etc.) rather than a real end-to-end Drive round-trip. For a
# structure-only check, pass --skip-website-docs, keep --dry-run (the
# default, so the Drive upload step never runs), or use act's own -n/--list
# flags via "--" below to just print the plan.
#
# Usage:
#   ./run-build-kotlin-docs-with-act.sh [options] [-- <extra act args>]
#
# Options:
#   --live                     Set dry_run=false (workflow default: true) -
#                              also needed for the "build complete" Slack
#                              notification to fire, see above
#   --skip-website-docs        Set skip_website_docs=true (default: false)
#   --kotlin-web-site-ref REF  kotlin_web_site_ref input (default: '')
#   --kotlin-ref REF           kotlin_ref input (default: '')
#   --images-zip-file-id ID    images_zip_file_id input (default: '')
#
# Secrets, read from the environment and passed through to act via a
# temporary secrets file - all five configured on the real repo, and this
# script refuses to run without any of them:
#   GCP_WIF_PROVIDER, GCP_WIF_SERVICE_ACCOUNT, GOOGLE_DRIVE_FILE_ID,
#   SLACK_WEBHOOK_URL, GOOGLE_DRIVE_IMAGES_ZIP_FILE_ID
# (the workflow itself treats the last two as optional - falling back to
# skipping the Slack notification, or to the images_zip_file_id input, if
# unset - but this script requires all five up front for a predictable run.)
set -euo pipefail

if ! command -v act >/dev/null 2>&1; then
  echo "error: act is required - see https://github.com/nektos/act#installation" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW="$REPO_ROOT/.github/workflows/build-kotlin-docs.yaml"

# --- workflow_dispatch inputs (mirrors this workflow's own defaults) ------
KOTLIN_WEB_SITE_REF=""
KOTLIN_REF=""
IMAGES_ZIP_FILE_ID=""
SKIP_WEBSITE_DOCS="false"
DRY_RUN="true"

EXTRA_ACT_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --live) DRY_RUN="false"; shift ;;
    --skip-website-docs) SKIP_WEBSITE_DOCS="true"; shift ;;
    --kotlin-web-site-ref) KOTLIN_WEB_SITE_REF="$2"; shift 2 ;;
    --kotlin-ref) KOTLIN_REF="$2"; shift 2 ;;
    --images-zip-file-id) IMAGES_ZIP_FILE_ID="$2"; shift 2 ;;
    --) shift; EXTRA_ACT_ARGS+=("$@"); break ;;
    *) echo "error: unrecognized argument '$1'" >&2; exit 1 ;;
  esac
done

# --- secrets, pulled from the environment if present ----------------------
SECRETS_FILE="$(mktemp)"
trap 'rm -f "$SECRETS_FILE"' EXIT

REQUIRED_SECRETS=(GCP_WIF_PROVIDER GCP_WIF_SERVICE_ACCOUNT GOOGLE_DRIVE_FILE_ID
                   SLACK_WEBHOOK_URL GOOGLE_DRIVE_IMAGES_ZIP_FILE_ID)
MISSING_REQUIRED=()

for name in "${REQUIRED_SECRETS[@]}"; do
  value="${!name:-}"
  if [ -n "$value" ]; then
    printf '%s=%s\n' "$name" "$value" >> "$SECRETS_FILE"
  else
    MISSING_REQUIRED+=("$name")
  fi
done

if [ "${#MISSING_REQUIRED[@]}" -gt 0 ]; then
  echo "error: missing required secret(s): ${MISSING_REQUIRED[*]}" >&2
  echo "error: these are the exact names appdevforall/OfflineDocumentationTools has" >&2
  echo "error: configured for this workflow on GitHub - GitHub never exposes a" >&2
  echo "error: secret's stored value through any API or CLI, so export your own" >&2
  echo "error: matching value for each one (e.g. 'export GOOGLE_DRIVE_FILE_ID=...')" >&2
  echo "error: before running this script." >&2
  exit 1
fi

if [ "$DRY_RUN" = "true" ]; then
  echo "note: dry_run=true - the 'build started' Slack notification will still fire," >&2
  echo "note: but 'build complete' is gated on dry_run=false and will be skipped;" >&2
  echo "note: pass --live if you want to see it." >&2
fi

echo "== Running $WORKFLOW via act (dry_run=$DRY_RUN, skip_website_docs=$SKIP_WEBSITE_DOCS) =="
act workflow_dispatch \
  -W "$WORKFLOW" \
  --input kotlin_web_site_ref="$KOTLIN_WEB_SITE_REF" \
  --input kotlin_ref="$KOTLIN_REF" \
  --input images_zip_file_id="$IMAGES_ZIP_FILE_ID" \
  --input skip_website_docs="$SKIP_WEBSITE_DOCS" \
  --input dry_run="$DRY_RUN" \
  --secret-file "$SECRETS_FILE" \
  "${EXTRA_ACT_ARGS[@]}"
