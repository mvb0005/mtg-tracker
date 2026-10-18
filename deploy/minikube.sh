#!/usr/bin/env bash
# Run mtgtracker on minikube. See README "Running on minikube".
#
#   deploy/minikube.sh start              bring the cluster back after a reboot (needs Docker running); no rebuild or redeploy
#   deploy/minikube.sh up                 build the image into minikube, apply manifests, wait
#   deploy/minikube.sh load-db [DB]       copy a local DB (default data/mtgtracker.db) + receipts/ into the pod
#   deploy/minikube.sh pull-db [DB]       copy the pod's DB back to a local file (old one kept as a backup)
#   deploy/minikube.sh import FILE.order  run `mtg import-order` inside the pod against the cluster DB
#   deploy/minikube.sh forward [PORT]     port-forward the UI to http://127.0.0.1:PORT (default 8000), reconnecting across redeploys
#   deploy/minikube.sh tunnel             expose the app via Cloudflare Tunnel (token from stdin, .env, or a prompt)
#   deploy/minikube.sh tunnel-down        stop the tunnel connector and delete its token
#   deploy/minikube.sh down               delete the app but keep the data volume
#   deploy/minikube.sh destroy            delete everything, including the data volume
set -euo pipefail

cd "$(dirname "$0")/.."

NS=mtgtracker
IMAGE=mtgtracker:dev
DATA=/data
# kubectl if installed, else minikube's bundled one (WSL often has only minikube).
if command -v kubectl >/dev/null 2>&1; then
  KUBECTL_BASE=(kubectl)
else
  KUBECTL_BASE=(minikube kubectl --)
fi
KUBECTL=("${KUBECTL_BASE[@]}" --namespace "$NS")

# Value of KEY from the repo's .env (gitignored). The file is parsed, never
# sourced/executed: optional "export", surrounding quotes and whitespace are
# stripped. Empty if unset.
env_value() {
  [[ -f .env ]] || return 0
  local v
  v=$(sed -n "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}$1[[:space:]]*=[[:space:]]*//p" .env | tail -n 1 | tr -d '\r')
  v=${v%"${v##*[![:space:]]}"}
  if [[ ${#v} -ge 2 && ( $v == \"*\" || $v == \'*\' ) ]]; then
    v=${v:1:${#v}-2}
  fi
  printf '%s' "$v"
}

pod() {
  "${KUBECTL[@]}" get pod -l app=mtgtracker -o jsonpath='{.items[0].metadata.name}'
}

# Sign-in settings for the app (web/auth.py), from .env, as the
# mtgtracker-auth ConfigMap the Deployment reads. MTG_AUTH defaults to
# single (no sign-in); cloudflare also needs the Access team domain and AUD.
# Checked here so a bad value fails the deploy instead of crash-looping the
# pod. None of these are secrets (the AUD is an identifier, not a key).
apply_auth_config() {
  local mode domain aud owner
  mode=$(env_value MTG_AUTH); mode=${mode:-single}
  domain=$(env_value CF_ACCESS_TEAM_DOMAIN)
  aud=$(env_value CF_ACCESS_AUD)
  owner=$(env_value MTG_OWNER_EMAIL)
  case "$mode" in
    single) ;;
    cloudflare)
      if [[ -z $domain || -z $aud ]]; then
        echo "MTG_AUTH=cloudflare needs CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD in .env" >&2
        exit 1
      fi
      [[ -n $owner ]] || echo "warning: no MTG_OWNER_EMAIL in .env; nobody can sign in as the owner" >&2
      ;;
    *)
      # dev lets anyone pick who they are -- never on the cluster.
      echo "MTG_AUTH must be single or cloudflare on the cluster, not '$mode'" >&2
      exit 1
      ;;
  esac
  "${KUBECTL[@]}" create configmap mtgtracker-auth \
    --from-literal=MTG_AUTH="$mode" \
    --from-literal=CF_ACCESS_TEAM_DOMAIN="$domain" \
    --from-literal=CF_ACCESS_AUD="$aud" \
    --from-literal=MTG_OWNER_EMAIL="$owner" \
    --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f -
  echo "auth: $mode"
}

cmd_up() {
  minikube status >/dev/null 2>&1 || minikube start
  minikube image build -t "$IMAGE" .
  "${KUBECTL_BASE[@]}" apply -k deploy/k8s
  apply_auth_config
  # Pick up a rebuilt image under the same tag.
  "${KUBECTL[@]}" rollout restart deployment/mtgtracker
  "${KUBECTL[@]}" rollout status deployment/mtgtracker --timeout=120s
}

# Bring an existing cluster back after a reboot. Builds nothing, applies
# nothing, restarts nothing: the pods come back from the saved cluster state.
cmd_start() {
  if ! docker version >/dev/null 2>&1; then
    echo "Docker isn't running. Start Docker Desktop, wait until it says it's running, then retry." >&2
    echo "From WSL: cmd.exe /c start \"\" \"C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe\"" >&2
    exit 1
  fi
  minikube start
  "${KUBECTL[@]}" wait --for=condition=Ready pod --all --timeout=180s
  "${KUBECTL[@]}" get pods
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

cmd_tunnel() {
  # The token is read from stdin (piped, or typed at a hidden prompt) and
  # handed to kubectl on stdin, so it never appears in argv, shell history,
  # or any file in the repo.
  local token=""
  if [[ ! -t 0 ]]; then
    read -r token || true  # piped in; no trailing newline is fine
  fi
  if [[ -z $token ]]; then
    token=$(env_value CLOUDFLARE_TUNNEL_TOKEN)
  fi
  if [[ -z $token && -t 0 ]]; then
    read -rsp "Cloudflare tunnel token: " token
    echo
  fi
  if [[ -z $token ]]; then
    echo "no token given. Set CLOUDFLARE_TUNNEL_TOKEN in .env (see .env.example), pipe it in," >&2
    echo "or run this in a terminal to get a hidden prompt." >&2
    exit 1
  fi

  printf '%s' "$token" \
    | "${KUBECTL[@]}" create secret generic cloudflared-token --from-file=token=/dev/stdin \
      --dry-run=client -o yaml \
    | "${KUBECTL_BASE[@]}" apply -f -
  unset token
  "${KUBECTL_BASE[@]}" apply -k deploy/k8s/tunnel
  # Pick up a changed token.
  "${KUBECTL[@]}" rollout restart deployment/cloudflared
  "${KUBECTL[@]}" rollout status deployment/cloudflared --timeout=120s
  echo "Tunnel connector running. Check it with: kubectl -n $NS logs deploy/cloudflared"
}

cmd_tunnel_down() {
  "${KUBECTL_BASE[@]}" delete -k deploy/k8s/tunnel --ignore-not-found
  "${KUBECTL[@]}" delete secret cloudflared-token --ignore-not-found
}

cmd_down() {
  "${KUBECTL[@]}" delete deployment/mtgtracker service/mtgtracker --ignore-not-found
}

cmd_destroy() {
  "${KUBECTL_BASE[@]}" delete -k deploy/k8s --ignore-not-found
}

case ${1:-} in
  start) cmd_start ;;
  up) cmd_up ;;
  load-db) shift; cmd_load_db "$@" ;;
  pull-db) shift; cmd_pull_db "$@" ;;
  import) shift; cmd_import "$@" ;;
  forward) shift; cmd_forward "$@" ;;
  tunnel) cmd_tunnel ;;
  tunnel-down) cmd_tunnel_down ;;
  down) cmd_down ;;
  destroy) cmd_destroy ;;
  *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
