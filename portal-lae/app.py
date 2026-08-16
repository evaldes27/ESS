"""
Portal de obra — Los Amigos Energy
Lee un tablero de monday.com y lo sirve como portal para el cliente.

Nada se captura aquí: monday es la única fuente de verdad.
Este servicio solo lee, normaliza y presenta.
"""

import json
import os
import time
from datetime import datetime

import requests
from flask import Flask, abort, redirect, render_template

app = Flask(__name__)

MONDAY_URL = "https://api.monday.com/v2"
MONDAY_TOKEN = os.environ["MONDAY_TOKEN"]
API_VERSION = os.environ.get("MONDAY_API_VERSION", "2025-01")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "300"))  # 5 min

# PORTALES mapea token secreto -> config del proyecto.
# El cliente entra a /o/<token>. Sin token no hay acceso.
# Ejemplo del .env:
#   PORTALES={"a7f3...": {"board_id": "18423973736", "nombre": "Casa Juan Pablo",
#                         "cliente": "Juan Pablo", "ubicacion": "Tulum, Q. Roo"}}
PORTALES = json.loads(os.environ["PORTALES"])

_cache = {}  # board_id -> (timestamp, datos)


# ---------------------------------------------------------------- monday

QUERY = """
query ($boardId: ID!) {
  boards(ids: [$boardId]) {
    id
    name
    groups { id title position }
    items_page(limit: 200) {
      items {
        id
        name
        updated_at
        group { id title }
        column_values {
          id
          type
          text
        }
        updates(limit: 20) {
          id
          body
          text_body
          created_at
          creator { name }
          assets { id name file_extension }
        }
      }
    }
  }
}
"""


def monday(query, variables=None):
    r = requests.post(
        MONDAY_URL,
        json={"query": query, "variables": variables or {}},
        headers={
            "Authorization": MONDAY_TOKEN,
            "API-Version": API_VERSION,
            "Content-Type": "application/json",
        },
        timeout=25,
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


# ------------------------------------------------------- normalización

# monday tiene etiquetas mezcladas en inglés y español. Aquí se unifican.
ESTADOS = {
    "done": "completa",
    "terminado": "completa",
    "listo": "completa",
    "working on it": "en_curso",
    "en proceso": "en_curso",
    "trabajando": "en_curso",
}

MESES = ["ene", "feb", "mar", "abr", "may", "jun",
         "jul", "ago", "sep", "oct", "nov", "dic"]


def normaliza_estado(texto):
    if not texto:
        return "pendiente"
    return ESTADOS.get(texto.strip().lower(), "pendiente")


def dia(iso):
    """2026-08-14 -> 14 ago"""
    try:
        d = datetime.strptime(iso.strip(), "%Y-%m-%d")
        return f"{d.day} {MESES[d.month - 1]}"
    except (ValueError, AttributeError):
        return None


def rango(texto):
    """'2026-08-14 - 2026-08-18' -> '14 ago – 18 ago'. Un solo día -> '14 ago'."""
    if not texto:
        return None
    partes = [p for p in texto.split(" - ") if p.strip()]
    fechas = [dia(p) for p in partes]
    fechas = [f for f in fechas if f]
    if not fechas:
        return None
    if len(fechas) == 1 or fechas[0] == fechas[-1]:
        return fechas[0]
    return f"{fechas[0]} – {fechas[-1]}"


def col(item, col_id):
    for c in item["column_values"]:
        if c["id"] == col_id:
            return c["text"]
    return None


def col_tipo(item, tipo):
    for c in item["column_values"]:
        if c["type"] == tipo:
            return c["text"]
    return None


def estado_fase(tareas):
    estados = [t["estado"] for t in tareas]
    if estados and all(e == "completa" for e in estados):
        return "completa"
    if any(e in ("completa", "en_curso") for e in estados):
        return "en_curso"
    return "pendiente"


def construir(board_id):
    """Trae el tablero y lo convierte en la forma que consume la plantilla."""
    data = monday(QUERY, {"boardId": str(board_id)})
    boards = data.get("boards") or []
    if not boards:
        raise RuntimeError(f"El tablero {board_id} no existe o el token no lo alcanza")
    board = boards[0]

    orden = {g["id"]: i for i, g in enumerate(board["groups"])}
    fases = {g["id"]: {"id": g["id"], "nombre": g["title"], "tareas": []}
             for g in board["groups"]}

    bitacora = []
    ultimo = None

    for item in board["items_page"]["items"]:
        gid = (item.get("group") or {}).get("id")
        if gid not in fases:
            continue

        # boolean_mkpw5syn = "Visible al cliente".
        # Si la columna existe y está desmarcada, la tarea no sale en el portal.
        # El checkbox "Ocultar al cliente": marcado = no se muestra.
        # Sin marcar o vacio = se muestra. El default es mostrar.
        oculto = (col(item, "boolean_mkpw5syn") or "").strip().lower()
        visible_cliente = oculto not in ("v", "true", "si", "yes", "1", "checked")

        tarea = {
            "id": item["id"],
            "nombre": item["name"],
            "estado": normaliza_estado(col(item, "project_status")),
            "fechas": rango(col(item, "project_timeline")),
            "quien": col(item, "project_owner"),
            "visible": visible_cliente,
        }
        fases[gid]["tareas"].append(tarea)

        if item["updated_at"] and (ultimo is None or item["updated_at"] > ultimo):
            ultimo = item["updated_at"]

        # Cada update de monday es una entrada de bitácora.
        for up in item.get("updates") or []:
            if not visible_cliente:
                continue
            fotos = [
                {"id": a["id"], "nombre": a["name"]}
                for a in (up.get("assets") or [])
                if (a.get("file_extension") or "").lower().lstrip(".")
                in ("jpg", "jpeg", "png", "heic", "webp")
            ]
            texto = (up.get("text_body") or "").strip()
            if not texto and not fotos:
                continue
            bitacora.append({
                "id": up["id"],
                "fase": fases[gid]["nombre"],
                "tarea": item["name"],
                "texto": texto,
                "quien": (up.get("creator") or {}).get("name"),
                "creado": up["created_at"],
                "fecha": dia((up["created_at"] or "")[:10]),
                "fotos": fotos,
            })

    lista = sorted(fases.values(), key=lambda f: orden.get(f["id"], 99))
    lista = [f for f in lista if f["tareas"]]
    for f in lista:
        f["visibles"] = [t for t in f["tareas"] if t["visible"]]
        f["estado"] = estado_fase(f["visibles"] or f["tareas"])
        f["hechas"] = sum(1 for t in f["visibles"] if t["estado"] == "completa")
        f["total"] = len(f["visibles"])

    lista = [f for f in lista if f["total"] > 0]
    bitacora.sort(key=lambda b: b["creado"], reverse=True)

    total = sum(f["total"] for f in lista)
    hechas = sum(f["hechas"] for f in lista)

    # El conducto se llena hasta la última fase con actividad.
    activa = 0
    for i, f in enumerate(lista):
        if f["estado"] != "pendiente":
            activa = i
    pct = (activa / (len(lista) - 1) * 100) if len(lista) > 1 else 0

    return {
        "board_id": board["id"],
        "fases": lista,
        "bitacora": bitacora[:30],
        "total": total,
        "hechas": hechas,
        "pct": pct,
        "actualizado": dia((ultimo or "")[:10]),
    }


def datos(board_id):
    ahora = time.time()
    hit = _cache.get(board_id)
    if hit and ahora - hit[0] < CACHE_TTL:
        return hit[1]
    frescos = construir(board_id)
    _cache[board_id] = (ahora, frescos)
    return frescos


# ------------------------------------------------------------- rutas

@app.route("/o/<token>")
def portal(token):
    cfg = PORTALES.get(token)
    if not cfg:
        abort(404)
    try:
        d = datos(cfg["board_id"])
    except Exception as e:
        app.logger.exception("Falló la lectura de monday")
        # Si monday no responde pero hay caché viejo, se sirve el viejo.
        hit = _cache.get(cfg["board_id"])
        if not hit:
            abort(503)
        d = hit[1]
    return render_template("portal.html", p=cfg, d=d, token=token)


@app.route("/o/<token>/foto/<asset_id>")
def foto(token, asset_id):
    """monday firma las URLs de archivo y expiran. Se pide una fresca al vuelo."""
    if token not in PORTALES:
        abort(404)
    q = "query ($ids: [ID!]!) { assets(ids: $ids) { id public_url } }"
    try:
        res = monday(q, {"ids": [str(asset_id)]})
        assets = res.get("assets") or []
        if not assets or not assets[0].get("public_url"):
            abort(404)
        return redirect(assets[0]["public_url"])
    except Exception:
        app.logger.exception("No se pudo resolver el asset %s", asset_id)
        abort(404)


@app.route("/healthz")
def healthz():
    return {"ok": True, "portales": len(PORTALES)}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
