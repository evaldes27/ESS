#!/usr/bin/env bash
# Deploy de lae-portal (portal + autonomía unificados) desde el Mac al VPS.
# Uso: ./deploy.sh [app...]   (sin argumento = lae-portal)

set -euo pipefail

VPS_HOST="lae-vps"
VPS_BASE="/opt"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APPS=("lae-portal")
if [[ $# -ge 1 ]]; then
  APPS=("$@")
fi

for app in "${APPS[@]}"; do
  if [[ ! -d "$REPO_DIR/$app" ]]; then
    echo "No existe $REPO_DIR/$app, saltando." >&2
    continue
  fi

  echo "==> Sincronizando $app..."
  rsync -avz --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='.env.example' \
    --exclude='.git' \
    "$REPO_DIR/$app/" "$VPS_HOST:$VPS_BASE/$app/"

  echo "==> Reconstruyendo contenedor de $app en el VPS..."
  ssh "$VPS_HOST" "cd $VPS_BASE/$app && docker compose up -d --build"

  echo "==> $app desplegado."
done

echo "Listo."
