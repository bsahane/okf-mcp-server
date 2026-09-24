#!/bin/sh
# Fetch the GitLab Handbook's People Group and Total Rewards sections at a
# pinned commit, for the Phase 1 pilot. Content stays outside this repository.
# Usage: samples/gitlab-handbook/fetch.sh [TARGET_DIR]   (default /tmp/gl-handbook)
set -eu
COMMIT=67bc662bf3f5d3f1c3cbf290ead2d6027341155d
TARGET=${1:-/tmp/gl-handbook}
if [ -e "$TARGET" ] && [ ! -d "$TARGET/.git" ]; then
  echo "refusing to use $TARGET: it exists and is not a git checkout" >&2
  exit 1
fi
[ -d "$TARGET/.git" ] || git init -q "$TARGET"
cd "$TARGET"
git remote get-url origin >/dev/null 2>&1 || git remote add origin https://gitlab.com/gitlab-com/content-sites/handbook.git
git sparse-checkout set content/handbook/people-group content/handbook/total-rewards
git fetch -q --depth 1 --filter=blob:none origin "$COMMIT"
git checkout -q FETCH_HEAD
echo "handbook sections at $COMMIT in $TARGET/content/handbook"
