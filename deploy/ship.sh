#!/usr/bin/env bash
# Ship mtg-tracker to minikube: tests, deploy, check the live pod, and say how to roll back.
# Run from Git Bash on Windows (the tests run here, in .venv); the deploy runs in WSL, where the cluster is.
#
#   deploy/ship.sh               refuse uncommitted changes, run the tests, deploy, check
#   deploy/ship.sh --rollback    put the previous build back (kubectl rollout undo) and check it
set -euo pipefail
cd "$(dirname "$0")/.."

win=$(pwd -W)                                   # C:/code/mtg-tracker
here="/mnt/${win:0:1}"; here="${here,,}${win:2}" # /mnt/c/code/mtg-tracker
in_wsl() { MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- bash -lc "cd '$here' && $1"; }
image() { in_wsl "kubectl -n mtgtracker get deployment mtgtracker -o jsonpath='{.spec.template.spec.containers[0].image}'" | tr -d '\r'; }

# The live pod must answer /healthz and render every deck page (the deck page reads the most: cards,
# the Scryfall cache, assembly, tokens).
check() {
  in_wsl "kubectl -n mtgtracker exec -i deployment/mtgtracker -- python -" <<'EOF'
import sqlite3, sys, urllib.error, urllib.request

def get(path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code

bad = []
if get("/healthz") != 200:
    bad.append("/healthz")
db = sqlite3.connect("file:/data/mtgtracker.db?mode=ro", uri=True)
decks = [row[0] for row in db.execute("SELECT id FROM decks")]
for deck in decks:
    if (status := get(f"/decks/{deck}")) != 200:
        bad.append(f"/decks/{deck} ({status})")
print(f"check: /healthz and {len(decks)} deck pages" + (": FAILED " + ", ".join(bad) if bad else ": ok"))
sys.exit(1 if bad else 0)
EOF
}

if [[ ${1:-} == --rollback ]]; then
  from=$(image)
  in_wsl "kubectl -n mtgtracker rollout undo deployment/mtgtracker && kubectl -n mtgtracker rollout status deployment/mtgtracker --timeout=120s"
  echo "Rolled back: $from -> $(image)"
  check
  exit
fi

[[ -z $(git status --porcelain -uno) ]] || { echo "Uncommitted changes: commit first, so the build is a commit you can revert." >&2; exit 1; }
.venv/Scripts/python -m pytest -q -x
prev=$(image)
in_wsl "bash deploy/minikube.sh up"
now=$(image)
check || { echo "The new build failed its check. Roll back: deploy/ship.sh --rollback (to $prev)" >&2; exit 1; }

prev_sha=$(sed -E 's/^mtgtracker:([0-9a-f]+)-.*/\1/' <<<"$prev")
echo
echo "Shipped $(git log -1 --format='%h %s') as $now"
echo "Roll back: deploy/ship.sh --rollback (to $prev), then git revert what you don't want before the next ship."
if git cat-file -e "$prev_sha^{commit}" 2>/dev/null; then
  if [[ -n $(git diff --name-only "$prev_sha" HEAD -- mtgtracker/migrations) ]]; then
    echo "Note: this build added a migration. The old code may not run on the migrated database; restore a"
    echo "restore point (deploy/minikube.sh pull-restore / restore) if a rollback misbehaves."
  fi
fi
