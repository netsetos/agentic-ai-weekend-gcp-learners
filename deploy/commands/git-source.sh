#!/usr/bin/env bash
# Source this file. It fetches repository source files; it never runs Terraform.
# Repository and branch selection belongs to the initial setup. Re-sourcing this
# helper preserves an explicitly configured source and an existing saved session.

rag_refresh_source() {
  local snapshot previous_snapshot=${SOURCE_COMMIT:-}
  unset SOURCE_COMMIT GIT_SHA
  : "${RAG_SOURCE_REPO:?Set the source repository directory}"
  : "${SOURCE_BRANCH:?Set the Git branch name}"
  : "${DEMO_ROOT:?Set the existing or new deployment directory}"
  git check-ref-format "refs/heads/$SOURCE_BRANCH" >/dev/null || return 1
  git -C "$RAG_SOURCE_REPO" fetch --no-tags origin \
    "+refs/heads/$SOURCE_BRANCH:refs/remotes/origin/$SOURCE_BRANCH" || return 1
  snapshot=$(git -C "$RAG_SOURCE_REPO" rev-parse --verify \
    "refs/remotes/origin/$SOURCE_BRANCH^{commit}") || return 1
  git -C "$RAG_SOURCE_REPO" cat-file -e \
    "$snapshot:deploy/terraform/backend.tf" || return 1
  export SOURCE_COMMIT="$snapshot" GIT_SHA="$snapshot"
  mkdir -p "$DEMO_ROOT" || return 1
  export RAG_SOURCE_BACKUP
  RAG_SOURCE_BACKUP=$(mktemp -d "${RAG_BACKUP_ROOT:-$HOME}/rag-source-backup.XXXXXXXX") || return 1
  # Save quoted values rather than sourcing data returned by Git.
  (umask 077
   printf 'export %s=%q\n' \
     SOURCE_BRANCH "$SOURCE_BRANCH" SOURCE_COMMIT "$SOURCE_COMMIT" \
     GIT_SHA "$GIT_SHA" RAG_SOURCE_REPO "$RAG_SOURCE_REPO" \
     DEMO_ROOT "$DEMO_ROOT" RAG_SOURCE_BACKUP "$RAG_SOURCE_BACKUP" \
     > "${RAG_SESSION_FILE:-$HOME/rag-source-session.env}") || return 1
  printf 'Source branch: %s\nSnapshot for this run: %s\nBackups: %s\n' \
    "$SOURCE_BRANCH" "$SOURCE_COMMIT" "$RAG_SOURCE_BACKUP"
  if [[ -n "$previous_snapshot" && "$previous_snapshot" != "$SOURCE_COMMIT" ]]; then
    printf '%s\n' 'Branch advanced. Rerun the source-file steps before building or planning.'
  fi
}

rag_get_file() (
  set -euo pipefail
  : "${DEMO_ROOT:?Complete source setup}"
  : "${RAG_SOURCE_REPO:?Complete source setup}"
  : "${SOURCE_COMMIT:?Run rag_refresh_source first}"
  : "${RAG_SOURCE_BACKUP:?Run rag_refresh_source first}"
  if [[ $# != 1 ]]; then
    printf '%s\n' "Usage: rag_get_file 'terraform/sink.tf' (no fixed SHA-256 argument)" >&2
    exit 1
  fi
  local relative=$1 temporary mode blob parent actual_parent root backup
  case "$relative" in
    ''|/*|..|../*|*/../*|*/..|*\\*|*$'\n'*|*$'\r'*)
      printf '%s\n' 'STOP: invalid relative source path.' >&2; exit 1 ;;
  esac
  [[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]
  cd "$DEMO_ROOT"
  root=$(pwd -P)
  parent=$(dirname "$relative")
  actual_parent=$(realpath -m -- "$parent")
  case "$actual_parent/" in
    "$root/"*) ;;
    *) printf '%s\n' 'STOP: source destination escapes the deployment directory.' >&2; exit 1 ;;
  esac
  mode=$(git -C "$RAG_SOURCE_REPO" ls-tree "$SOURCE_COMMIT" -- "deploy/$relative" | awk '{print $1}')
  case "$mode" in
    100644|100755) ;;
    *) printf 'STOP: source is absent or not a regular file: %s\n' "$relative" >&2; exit 1 ;;
  esac
  blob=$(git -C "$RAG_SOURCE_REPO" rev-parse --verify "$SOURCE_COMMIT:deploy/$relative")
  mkdir -p -- "$parent"
  temporary=$(mktemp "$parent/.rag-source.XXXXXXXX")
  trap 'rm -f "$temporary"' EXIT
  git -C "$RAG_SOURCE_REPO" show "$SOURCE_COMMIT:deploy/$relative" > "$temporary"
  test "$(git -C "$RAG_SOURCE_REPO" hash-object --stdin < "$temporary")" = "$blob"
  if [[ -e "$relative" || -L "$relative" ]]; then
    if [[ -L "$relative" || ! -f "$relative" ]]; then
      printf 'STOP: destination is not a regular file: %s\n' "$relative" >&2
      exit 1
    fi
    if cmp -s "$temporary" "$relative"; then
      chmod "${mode#100}" "$relative"
      printf 'REUSE: %s\n' "$relative"
      exit 0
    fi
    mkdir -p -- "$RAG_SOURCE_BACKUP/$parent"
    backup=$(mktemp "$RAG_SOURCE_BACKUP/$relative.XXXXXXXX")
    cp -p -- "$relative" "$backup"
    printf 'BACKUP: %s\n' "$backup"
  fi
  chmod "${mode#100}" "$temporary"
  mv -- "$temporary" "$relative"
  printf 'FETCHED: %s (Git blob verified)\n' "$relative"
)
