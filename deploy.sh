#!/usr/bin/env bash
# Deploy portal-lae y/o autonomia-lae desde el Mac al VPS.
# Uso: ./deploy.sh [portal-lae|autonomia-lae]   (sin argumento = ambas)

set -euo pipefail

VPS_HOST="lae-vps"
VPS_BASE="/opt"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APPS=("portal-lae" "autonomia-lae")
if [[ $# -eq 1 ]]; then
  APPS=("$1")
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
