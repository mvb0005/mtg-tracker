#!/usr/bin/env bash
# Run mtgtracker on minikube. See README "Running on minikube".
#
#   deploy/minikube.sh up                 build the image into minikube, apply manifests, wait
#   deploy/minikube.sh load-db [DB]       copy a local DB (default data/mtgtracker.db) + receipts/ into the pod
#   deploy/minikube.sh pull-db [DB]       copy the pod's DB back to a local file (old one kept as a backup)
#   deploy/minikube.sh import FILE.order  run `mtg import-order` inside the pod against the cluster DB
#   deploy/minikube.sh forward [PORT]     port-forward the UI to http://127.0.0.1:PORT (default 8000), reconnecting across redeploys
#   deploy/minikube.sh down               delete the app but keep the data volume
#   deploy/minikube.sh destroy            delete everything, including the data volume
set -euo pipefail

cd "$(dirname "$0")/.."

NS=mtgtracker
IMAGE=mtgtracker:dev
DATA=/data
KUBECTL=(kubectl --namespace "$NS")

pod() {
  "${KUBECTL[@]}" get pod -l app=mtgtracker -o jsonpath='{.items[0].metadata.name}'
}

cmd_up() {
  minikube status >/dev/null 2>&1 || minikube start
  minikube image build -t "$IMAGE" .
  kubectl apply -k deploy/k8s
  # Pick up a rebuilt image under the same tag.
  "${KUBECTL[@]}" rollout restart deployment/mtgtracker
  "${KUBECTL[@]}" rollout status deployment/mtgtracker --timeout=120s
}

cmd_load_db() {
  local src=${1:-data/mtgtracker.db}
  [[ -f $src ]] || { echo "no database at $src" >&2; exit 1; }

  local tmp
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' RETURN

  # Consistent snapshot via SQLite's online backup, not a raw file copy.
  python3 - "$src" "$tmp/snapshot.db" <<'EOF'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
EOF

  local p
  p=$(pod)
  # Copy to a temp name, then rename: requests never see a half-written file.
  "${KUBECTL[@]}" cp "$tmp/snapshot.db" "$p:$DATA/.mtgtracker.db.incoming"
  "${KUBECTL[@]}" exec "$p" -- mv "$DATA/.mtgtracker.db.incoming" "$DATA/mtgtracker.db"

  if compgen -G "receipts/*.order" >/dev/null; then
    "${KUBECTL[@]}" exec "$p" -- mkdir -p "$DATA/receipts"
    for f in receipts/*.order; do
      "${KUBECTL[@]}" cp "$f" "$p:$DATA/receipts/$(basename "$f")"
    done
  fi
  echo "Loaded $src into $p:$DATA/mtgtracker.db"
}

cmd_pull_db() {
  local dest=${1:-data/mtgtracker.db}
  local p
  p=$(pod)

  # Consistent snapshot inside the pod, then copy it out.
  "${KUBECTL[@]}" exec "$p" -- python -c "
import sqlite3
src, dst = sqlite3.connect('$DATA/mtgtracker.db'), sqlite3.connect('/tmp/pull.db')
src.backup(dst)
dst.close(); src.close()
"
  mkdir -p "$(dirname "$dest")"
  "${KUBECTL[@]}" cp "$p:/tmp/pull.db" "$dest.incoming" >/dev/null
  "${KUBECTL[@]}" exec "$p" -- rm -f /tmp/pull.db

  if [[ -f $dest ]]; then
    local backup="${dest%.db}.backup-$(date +%Y%m%d-%H%M%S).db"
    cp "$dest" "$backup"
    echo "Previous $dest kept as $backup"
  fi
  mv "$dest.incoming" "$dest"
  echo "Pulled $p:$DATA/mtgtracker.db into $dest"
}

cmd_import() {
  local file=${1:?usage: import FILE.order}
  local p
  p=$(pod)
  "${KUBECTL[@]}" cp "$file" "$p:/tmp/import.order"
  "${KUBECTL[@]}" exec "$p" -- mtg import-order /tmp/import.order \
    --db "$DATA/mtgtracker.db" --receipts "$DATA/receipts"
  "${KUBECTL[@]}" exec "$p" -- rm -f /tmp/import.order
}

cmd_forward() {
  local port=${1:-8000}
  echo "mtgtracker at http://127.0.0.1:$port (Ctrl-C to stop)"
  # port-forward exits whenever the pod is replaced (e.g. by `up`), so
  # reconnect until interrupted.
  trap 'exit 0' INT TERM
  while true; do
    "${KUBECTL[@]}" port-forward --address 127.0.0.1 service/mtgtracker "$port:80" || true
    echo "port-forward dropped; reconnecting..." >&2
    sleep 2
  done
}

cmd_down() {
  "${KUBECTL[@]}" delete deployment/mtgtracker service/mtgtracker --ignore-not-found
}

cmd_destroy() {
  kubectl delete -k deploy/k8s --ignore-not-found
}

case ${1:-} in
  up) cmd_up ;;
  load-db) shift; cmd_load_db "$@" ;;
  pull-db) shift; cmd_pull_db "$@" ;;
  import) shift; cmd_import "$@" ;;
  forward) shift; cmd_forward "$@" ;;
  down) cmd_down ;;
  destroy) cmd_destroy ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
