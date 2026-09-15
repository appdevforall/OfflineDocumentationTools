#!/usr/bin/env bash
# Shallow-checks-out one ref of a remote repository, where "ref" may be a branch, a tag, or a
# commit SHA.
#
# `git clone --depth 1 --branch <ref>` handles the first two and rejects the third outright
# ("Remote branch <sha> not found in upstream origin") - which is the case that matters, since
# pinning a SHA is what makes a build reproducible. `git fetch` takes all three.
#
#   shallow-checkout.sh <url> <directory> [ref]
#
# With no ref, the remote's default branch is used.
set -euo pipefail

URL="$1"
DIR="$2"
REF="${3:-}"

if [ -z "$REF" ]; then
  git clone --depth 1 "$URL" "$DIR"
  exit 0
fi

git init -q "$DIR"
git -C "$DIR" remote add origin "$URL"
# A SHA needs the remote to allow fetching it by object name; GitHub does. A branch or tag name
# resolves the same way, so there is no need to work out which kind this is.
git -C "$DIR" fetch --depth 1 origin "$REF"
git -C "$DIR" checkout -q FETCH_HEAD
echo "Checked out $REF from $URL at $(git -C "$DIR" rev-parse HEAD)"
