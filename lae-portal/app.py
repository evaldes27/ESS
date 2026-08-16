"""
Portal unificado — Los Amigos Energy

Junta dos fuentes de datos por cliente:
  - monday.com: specs, avance de obra, bitácora con fotos (portal-lae)
  - VRM (Victron): autonomía en vivo (autonomia-lae)

Nada se captura aquí: monday y VRM son la única fuente de verdad para
esos datos. Documentos y saldo son config manual por cliente.

Rutas:
  /o/<token>              portal de obra viejo (monday) — no tocar, ya está en manos de clientes
  /a/<token>              autonomía en vivo vieja (VRM) — no tocar, ya está en manos de clientes
  /mi/<token>             página unificada nueva, por cliente (CLIENTES)
  /mi/<token>/datos       polling en vivo de VRM para /mi
  /mi/<token>/foto/<id>   fotos de bitácora para /mi
"""

import json
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, abort, jsonify, redirect, render_template

app = Flask(__name__)


# ================================================================== monday
# (de portal-lae, sin cambios de comportamiento)

MONDAY_URL = "https://api.monday.com/v2"
MONDAY_TOKEN = os.environ["MONDAY_TOKEN"]
API_VERSION = os.environ.get("MONDAY_API_VERSION", "2025-01")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "300"))  # 5 min

# PORTALES mapea token secreto -> config del proyecto. Ruta vieja /o/<token>.
# Ejemplo del .env:
#   PORTALES={"a7f3...": {"board_id": "18423973736", "nombre": "Casa Juan Pablo",
#                         "cliente": "Juan Pablo", "ubicacion": "Tulum, Q. Roo"}}
PORTALES = json.loads(os.environ["PORTALES"])

_cache_monday = {}  # board_id -> (timestamp, datos)

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


def construir_monday(board_id):
    """Trae el tablero y lo convierte en la forma que consumen las plantillas."""
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


def datos_monday(board_id):
    ahora = time.time()
    hit = _cache_monday.get(board_id)
    if hit and ahora - hit[0] < CACHE_TTL:
        return hit[1]
    frescos = construir_monday(board_id)
    _cache_monday[board_id] = (ahora, frescos)
    return frescos


def resolver_foto(asset_id):
    """monday firma las URLs de archivo y expiran. Se pide una fresca al vuelo."""
    q = "query ($ids: [ID!]!) { assets(ids: $ids) { id public_url } }"
    res = monday(q, {"ids": [str(asset_id)]})
    assets = res.get("assets") or []
    if not assets or not assets[0].get("public_url"):
        return None
    return assets[0]["public_url"]


# ====================================================================== VRM
# (de autonomia-lae, sin cambios de comportamiento)

VRM_URL = "https://vrmapi.victronenergy.com/v2"
VRM_TOKEN = os.environ["VRM_TOKEN"]
POLL_SEGUNDOS = int(os.environ.get("POLL_SEGUNDOS", "60"))
VENTANA_MINUTOS = int(os.environ.get("VENTANA_MINUTOS", "20"))

# SITIOS mapea token secreto -> config del sitio VRM. Ruta vieja /a/<token>.
SITIOS = json.loads(os.environ["SITIOS"])

TZ = timezone(timedelta(hours=-5))

_cache_vrm = {}
_historial = {}


def vrm(path, params=None):
    r = requests.get(
        f"{VRM_URL}{path}",
        params=params or {},
        headers={"X-Authorization": f"Token {VRM_TOKEN}"},
        timeout=25,
    )
    r.raise_for_status()
    return r.json()


def diagnostics(vrm_id):
    data = vrm(f"/installations/{vrm_id}/diagnostics", {"count": 200})
    out = {}
    for rec in data.get("records", []):
        code = rec.get("code")
        if code:
            out[code] = rec.get("rawValue", rec.get("formattedValue"))
    return out


def num(v, por_defecto=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return por_defecto


def promedio_potencia(vrm_id, watts_ahora):
    ahora = time.time()
    hist = _historial.setdefault(vrm_id, deque())
    hist.append((ahora, watts_ahora))
    corte = ahora - VENTANA_MINUTOS * 60
    while hist and hist[0][0] < corte:
        hist.popleft()
    if not hist:
        return watts_ahora
    return sum(w for _, w in hist) / len(hist)


def formato_horas(h):
    if h is None:
        return None
    if h >= 48:
        return "mas de 48 horas"
    hh = int(h)
    mm = int(round((h - hh) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    if hh == 0:
        return f"{mm} minutos"
    if mm == 0:
        return f"{hh} {'hora' if hh == 1 else 'horas'}"
    return f"{hh} h {mm:02d} min"


def hora_reloj(h):
    if h is None or h >= 48:
        return None
    fin = datetime.now(TZ) + timedelta(hours=h)
    hh = fin.hour
    ampm = "AM" if hh < 12 else "PM"
    h12 = hh % 12 or 12
    return f"{h12}:{fin.minute:02d} {ampm}"


def construir_vrm(cfg):
    vrm_id = cfg["vrm_id"]
    d = diagnostics(vrm_id)

    soc = num(d.get("bs"))
    soc_min = float(cfg.get("soc_minimo", 10))
    banco = float(cfg["banco_kwh"])

    consumo = sum(num(d.get(c)) for c in ("a1", "a2", "a3"))
    if consumo <= 0:
        consumo = sum(num(d.get(c)) for c in ("OP1", "OP2", "OP3"))

    solar = num(d.get("Pdc")) + num(d.get("PVP"))
    bateria_w = num(d.get("bp"))
    cargando = bateria_w > 20

    suave = promedio_potencia(vrm_id, consumo)

    disponible = banco * max(0.0, soc - soc_min) / 100.0
    if cargando or suave <= 50:
        horas = None
    else:
        horas = disponible / (suave / 1000.0)

    v_red = max(num(d.get("IV1")), num(d.get("IV2")))
    conectado = str(d.get("ic0", "")).lower() in ("1", "true", "connected")
    if conectado:
        red = "conectada"
    elif v_red > 50:
        red = "rechazada"
    else:
        red = "caida"

    return {
        "nombre": cfg.get("nombre", ""),
        "soc": round(soc),
        "consumo": int(round(consumo)),
        "consumo_suave": int(round(suave)),
        "solar": int(round(solar)),
        "cargando": cargando,
        "disponible": round(disponible, 1),
        "banco": banco,
        "horas": horas,
        "horas_txt": formato_horas(horas),
        "hasta": hora_reloj(horas),
        "red": red,
        "red_voltaje": round(v_red, 1) if v_red > 50 else None,
        "actualizado": datetime.now(TZ).strftime("%I:%M %p").lstrip("0"),
    }


def datos_vrm(cfg):
    vrm_id = cfg["vrm_id"]
    ahora = time.time()
    hit = _cache_vrm.get(vrm_id)
    if hit and ahora - hit[0] < POLL_SEGUNDOS:
        return hit[1]
    frescos = construir_vrm(cfg)
    _cache_vrm[vrm_id] = (ahora, frescos)
    return frescos


# ============================================================ /mi (nuevo)

# CLIENTES mapea token secreto -> config unificada del cliente. Ruta /mi/<token>.
# Cada cliente puede traer board_id (monday), vrm_id + banco_kwh (VRM), o ambos.
# "sistema", "documentos" y "saldo" son config manual, igual que "sistema" en PORTALES.
# Ejemplo del .env:
#   CLIENTES={"tok...": {"nombre": "Casa Juan Pablo", "cliente": "Juan Pablo",
#     "ubicacion": "Tulum, Q. Roo", "board_id": "18423973736",
#     "vrm_id": 901035, "banco_kwh": 30.72, "soc_minimo": 10,
#     "sistema": {...}, "documentos": [{"nombre": "Contrato", "url": "https://..."}],
#     "saldo": {"total": 450000, "pagado": 300000}}}
CLIENTES = json.loads(os.environ.get("CLIENTES", "{}"))


def orden_secciones(obra_abierta, energizado):
    """Las secciones que siempre existen no cambian; solo cambia cuál va primero."""
    secciones = []
    if energizado:
        secciones.append("autonomia")
    if obra_abierta:
        secciones.append("avance")
    secciones += ["documentos", "sistema", "bitacora"]
    return secciones


# ------------------------------------------------------------- rutas viejas

@app.route("/o/<token>")
def portal(token):
    cfg = PORTALES.get(token)
    if not cfg:
        abort(404)
    try:
        d = datos_monday(cfg["board_id"])
    except Exception:
        app.logger.exception("Falló la lectura de monday")
        # Si monday no responde pero hay caché viejo, se sirve el viejo.
        hit = _cache_monday.get(cfg["board_id"])
        if not hit:
            abort(503)
        d = hit[1]
    return render_template("portal.html", p=cfg, d=d, token=token)


@app.route("/o/<token>/foto/<asset_id>")
def foto(token, asset_id):
    if token not in PORTALES:
        abort(404)
    try:
        url = resolver_foto(asset_id)
    except Exception:
        app.logger.exception("No se pudo resolver el asset %s", asset_id)
        url = None
    if not url:
        abort(404)
    return redirect(url)


@app.route("/a/<token>")
def pagina(token):
    cfg = SITIOS.get(token)
    if not cfg:
        abort(404)
    try:
        d = datos_vrm(cfg)
    except Exception:
        app.logger.exception("Fallo la lectura de VRM")
        hit = _cache_vrm.get(cfg["vrm_id"])
        if not hit:
            abort(503)
        d = hit[1]
    return render_template("autonomia.html", d=d, token=token)


@app.route("/a/<token>/datos")
def datos_json(token):
    cfg = SITIOS.get(token)
    if not cfg:
        abort(404)
    try:
        return jsonify(datos_vrm(cfg))
    except Exception:
        hit = _cache_vrm.get(cfg["vrm_id"])
        if not hit:
            abort(503)
        return jsonify(hit[1])


# ------------------------------------------------------------- ruta nueva

@app.route("/mi/<token>")
def mi(token):
    cfg = CLIENTES.get(token)
    if not cfg:
        abort(404)

    obra_abierta = "board_id" in cfg
    energizado = "vrm_id" in cfg

    m = None
    if obra_abierta:
        try:
            m = datos_monday(cfg["board_id"])
        except Exception:
            app.logger.exception("Falló la lectura de monday para /mi/%s", token)
            hit = _cache_monday.get(cfg["board_id"])
            m = hit[1] if hit else None

    v = None
    if energizado:
        try:
            v = datos_vrm(cfg)
        except Exception:
            app.logger.exception("Falló la lectura de VRM para /mi/%s", token)
            hit = _cache_vrm.get(cfg["vrm_id"])
            v = hit[1] if hit else None

    return render_template(
        "mi.html",
        p=cfg,
        m=m,
        v=v,
        token=token,
        secciones=orden_secciones(obra_abierta, energizado),
    )


@app.route("/mi/<token>/datos")
def mi_datos(token):
    cfg = CLIENTES.get(token)
    if not cfg or "vrm_id" not in cfg:
        abort(404)
    try:
        return jsonify(datos_vrm(cfg))
    except Exception:
        hit = _cache_vrm.get(cfg["vrm_id"])
        if not hit:
            abort(503)
        return jsonify(hit[1])


@app.route("/mi/<token>/foto/<asset_id>")
def mi_foto(token, asset_id):
    if token not in CLIENTES:
        abort(404)
    try:
        url = resolver_foto(asset_id)
    except Exception:
        app.logger.exception("No se pudo resolver el asset %s", asset_id)
        url = None
    if not url:
        abort(404)
    return redirect(url)


@app.route("/healthz")
def healthz():
    return {"ok": True, "portales": len(PORTALES), "sitios": len(SITIOS), "clientes": len(CLIENTES)}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
