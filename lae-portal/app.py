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

import base64
import calendar
import json
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

import requests
from flask import Flask, Response, abort, jsonify, redirect, render_template
from PIL import Image, ImageOps
from xhtml2pdf import pisa

# static_url_path distinto de "/static": ese prefijo ya lo usa monitor-luz-tulum
# detrás del mismo nginx, y "/static/" a secas les pisaría sus imágenes.
app = Flask(__name__, static_url_path="/mi-static")


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


# ====================================================================== CFE
# (de monitor-luz-tulum/cfe_estimate.py + cfe_1d_estimador.html, portado a
# Python; mismo cálculo, ahora como tarjeta resumen en vez de widget interactivo.
# Tarifa 1D, zona Cancún/Q. Roo. Ajustar aquí si cambia el recibo real.)

CFE_CICLO_MESES = 2
CFE_GRACIA_DIAS = 4
CFE_IMPORT_KEYS = ("Gc", "Gb")
CFE_EXPORT_KEYS = ("Pg", "Bg")
CFE_MESES_VERANO = (5, 6, 7, 8, 9, 10)
CFE_CACHE_TTL = 600  # 10 min

CFE_TARIFAS = {
    "verano": [
        ("Básica", 350, 0.961),
        ("Intermedia 1", 800, 1.115),
        ("Intermedia 2", 1200, 1.435),
        ("Excedente", None, 3.833),
    ],
    "invierno": [
        ("Básica", 350, 0.961),
        ("Intermedia 1", 800, 1.115),
        ("Intermedia 2", 1200, 1.435),
        ("Excedente", None, 3.833),
    ],
}
CFE_CARGO_FIJO = 0.0
CFE_IVA = 0.16
CFE_DAP = 0.05
CFE_MINIMO = 65.0

_cache_cfe = {}


def cfe_temporada(fecha):
    return "verano" if fecha.month in CFE_MESES_VERANO else "invierno"


def cfe_suma_mes(fecha, meses):
    m = fecha.month - 1 + meses
    y = fecha.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(fecha.day, calendar.monthrange(y, m)[1]))


def cfe_periodo_actual(ancla, hoy):
    inicio = ancla
    fin = cfe_suma_mes(inicio, CFE_CICLO_MESES)
    while fin <= hoy:
        inicio, fin = fin, cfe_suma_mes(fin, CFE_CICLO_MESES)
    return inicio, fin


def cfe_vrm_kwh(vrm_id, inicio, fin):
    params = {
        "type": "kwh",
        "start": int(time.mktime(inicio.timetuple())),
        "end": int(time.mktime(fin.timetuple())),
        "interval": "days",
    }
    return vrm(f"/installations/{vrm_id}/stats", params)


def cfe_suma_clave(registros, clave):
    total = 0.0
    for pt in (registros.get(clave) or []):
        try:
            if pt[1] is not None:
                total += float(pt[1])
        except (TypeError, IndexError, ValueError):
            pass
    return total


def cfe_suma_claves(registros, claves):
    return sum(cfe_suma_clave(registros, k) for k in claves)


def cfe_energia(kwh, temporada):
    """Costo de energía por bloques (tarifa 1D), sin fijo/IVA/DAP/mínimo."""
    total, restante = 0.0, kwh
    prev_limite = 0
    for _nombre, limite, precio in CFE_TARIFAS[temporada]:
        tope = limite if limite is not None else float("inf")
        en_bloque = max(0.0, min(kwh, tope) - prev_limite)
        total += en_bloque * precio
        prev_limite = tope
    return total


def cfe_total(kwh, temporada):
    energia = cfe_energia(kwh, temporada)
    iva = (energia + CFE_CARGO_FIJO) * CFE_IVA
    dap = energia * CFE_DAP
    calculado = energia + CFE_CARGO_FIJO + iva + dap
    return max(calculado, CFE_MINIMO)


def construir_cfe(cfg):
    vrm_id = cfg["vrm_id"]
    bolsa = float(cfg["cfe_bolsa"])
    ancla = date.fromisoformat(cfg["cfe_ancla"])
    hoy = datetime.now(TZ).date()

    inicio, fin = cfe_periodo_actual(ancla, hoy)
    dias_transcurridos = max((hoy - inicio).days, 1)
    dias_totales = max((fin - inicio).days, dias_transcurridos)
    temporada = cfe_temporada(hoy)

    if dias_transcurridos <= CFE_GRACIA_DIAS:
        return {
            "recien_empezo": True,
            "temporada": temporada,
            "periodo": {"dias_transcurridos": dias_transcurridos, "dias_totales": dias_totales},
        }

    payload = cfe_vrm_kwh(vrm_id, inicio, hoy + timedelta(days=1))
    registros = payload.get("records") or {}
    importado = cfe_suma_claves(registros, CFE_IMPORT_KEYS)
    exportado = cfe_suma_claves(registros, CFE_EXPORT_KEYS)
    neto_proyectado = (importado - exportado) / dias_transcurridos * dias_totales

    if neto_proyectado >= 0:
        bolsa_despues = max(0.0, bolsa - neto_proyectado)
    else:
        bolsa_despues = bolsa + (-neto_proyectado)
    kwh_facturado = max(0.0, neto_proyectado - bolsa)

    return {
        "recien_empezo": False,
        "temporada": temporada,
        "neto_proyectado": round(neto_proyectado, 1),
        "bolsa": round(bolsa, 1),
        "bolsa_despues": round(bolsa_despues, 1),
        "kwh_facturado": round(kwh_facturado, 1),
        "total": round(cfe_total(kwh_facturado, temporada), 2),
        "periodo": {"dias_transcurridos": dias_transcurridos, "dias_totales": dias_totales},
    }


def datos_cfe(cfg):
    vrm_id = cfg["vrm_id"]
    ahora = time.time()
    hit = _cache_cfe.get(vrm_id)
    if hit and ahora - hit[0] < CFE_CACHE_TTL:
        return hit[1]
    frescos = construir_cfe(cfg)
    _cache_cfe[vrm_id] = (ahora, frescos)
    return frescos


# =============================================================== idiomas
# Solo interfaz (títulos, botones, estados). Los datos que llegan de monday
# (nombres de tarea, notas de bitácora) los escribe el equipo en sitio en
# español y se muestran tal cual, sin traducir.

TRADUCCIONES = {
    "es": {
        "tu_proyecto": "Tu proyecto",
        "autonomia_titulo": "Autonomía en vivo",
        "actualizado_a_las": "Actualizado a las {hora}",
        "bateria_cargando": "Tu batería se está cargando",
        "sol_cargando": "El sol está cargando tu batería en este momento.",
        "bateria_cargando_simple": "Tu batería está cargando.",
        "queda_energia": "Te queda de energía",
        "hasta_las": "Hasta las {hora}, con lo que estás usando ahorita.",
        "bateria_pct": "Batería {pct}%",
        "llena": "Llena",
        "tu_sistema": "Tu sistema",
        "bateria_sin_consumo": "Batería cargada. No hay consumo suficiente para calcular el tiempo restante.",
        "estas_usando": "Estás usando",
        "bateria": "Batería",
        "del_sol": "Del sol",
        "luz_baja_titulo": "La luz de la colonia está muy baja",
        "luz_baja_texto": "Hay corriente en la calle, pero llega tan baja que tu sistema la está "
                          "bloqueando para no dañar tus aparatos. Tu casa está funcionando con las baterías.",
        "luz_caida_titulo": "No hay luz de la calle",
        "luz_caida_texto": "Tu casa está funcionando con las baterías. Te avisamos aquí cuando regrese.",

        "cfe_titulo": "Tu próximo recibo de CFE",
        "cfe_temporada": {"verano": "Verano", "invierno": "Invierno"},
        "cfe_recien_empezo": "El periodo apenas comenzó, tu estimado estará listo en unos días.",
        "cfe_credito_cubre": "{bolsa} kWh de crédito — cubre todo tu consumo, solo pagas el mínimo.",
        "cfe_banco_aplicado": "{bolsa} kWh de banco aplicados · {facturado} kWh facturados · día {dia} de {dias_totales}",
        "cfe_estimado": "Estimado del periodo",

        "avance_titulo": "Avance de obra",
        "tareas_conteo": "{hechas} de {total} tareas",
        "tareas_terminadas": "{hechas} de {total} tareas terminadas",
        "sin_datos": "Sin datos por ahora",
        "sin_avance": "Todavía no hay datos de avance disponibles.",
        "total_proyecto": "Total del proyecto",
        "pagado": "Pagado",
        "saldo_pendiente": "Saldo pendiente",

        "documentos_titulo": "Documentos",
        "documentos_sub": "Contrato, planos y más",
        "documento_default": "Documento",
        "ver": "Ver",
        "sin_documentos": "Todavía no hay documentos disponibles aquí.",

        "seriales_titulo": "Seriales y garantías",
        "seriales_sub": "Guarda esto para cuando lo necesites",
        "col_equipo": "Equipo",
        "col_modelo": "Modelo",
        "col_serie": "Serie",
        "col_garantia": "Garantía hasta",
        "sin_seriales": "Todavía no hay seriales registrados para este sistema.",

        "bitacora_titulo": "Bitácora",
        "bitacora_sub": "Lo que pasó en sitio",
        "sin_bitacora": "Todavía no hay entradas. Aquí van a aparecer las fotos y notas de cada avance en sitio.",

        "equipo_de": "Equipo de",
        "pie_empresa": "Los Amigos Energy · Solar Energy Lat, S.A. de C.V.",
        "pie_privado": "Esta página es privada. No la compartas fuera de tu proyecto.",

        "reporte_pdf": "Descargar reporte PDF",
        "reporte_titulo": "Reporte de proyecto",
        "reporte_generado": "Generado el {fecha}",
        "reporte_pie": "Este documento es un respaldo generado automáticamente a partir del portal en línea. "
                       "Los datos reflejan el estado del proyecto al momento de generarlo.",
    },
    "en": {
        "tu_proyecto": "Your project",
        "autonomia_titulo": "Live autonomy",
        "actualizado_a_las": "Updated at {hora}",
        "bateria_cargando": "Your battery is charging",
        "sol_cargando": "The sun is charging your battery right now.",
        "bateria_cargando_simple": "Your battery is charging.",
        "queda_energia": "Energy remaining",
        "hasta_las": "Until {hora}, at your current usage.",
        "bateria_pct": "Battery {pct}%",
        "llena": "Full",
        "tu_sistema": "Your system",
        "bateria_sin_consumo": "Battery full. Not enough consumption to calculate remaining time.",
        "estas_usando": "You're using",
        "bateria": "Battery",
        "del_sol": "From the sun",
        "luz_baja_titulo": "Neighborhood power is too low",
        "luz_baja_texto": "There's power on the street, but it's arriving too low, so your system is "
                          "blocking it to protect your appliances. Your home is running on battery.",
        "luz_caida_titulo": "No power from the grid",
        "luz_caida_texto": "Your home is running on battery. We'll show it here when it's back.",

        "cfe_titulo": "Your next CFE bill",
        "cfe_temporada": {"verano": "Summer", "invierno": "Winter"},
        "cfe_recien_empezo": "The billing period just started — your estimate will be ready in a few days.",
        "cfe_credito_cubre": "{bolsa} kWh credit — fully covers your usage, minimum bill only.",
        "cfe_banco_aplicado": "{bolsa} kWh bank applied · {facturado} kWh billed · day {dia} of {dias_totales}",
        "cfe_estimado": "Period estimate",

        "avance_titulo": "Construction progress",
        "tareas_conteo": "{hechas} of {total} tasks",
        "tareas_terminadas": "{hechas} of {total} tasks completed",
        "sin_datos": "No data yet",
        "sin_avance": "No progress data available yet.",
        "total_proyecto": "Project total",
        "pagado": "Paid",
        "saldo_pendiente": "Balance due",

        "documentos_titulo": "Documents",
        "documentos_sub": "Contract, plans and more",
        "documento_default": "Document",
        "ver": "View",
        "sin_documentos": "No documents available here yet.",

        "seriales_titulo": "Serials and warranties",
        "seriales_sub": "Save this for when you need it",
        "col_equipo": "Equipment",
        "col_modelo": "Model",
        "col_serie": "Serial",
        "col_garantia": "Warranty until",
        "sin_seriales": "No serial numbers registered for this system yet.",

        "bitacora_titulo": "Log",
        "bitacora_sub": "What happened on site",
        "sin_bitacora": "No entries yet. Photos and notes from each site update will appear here.",

        "equipo_de": "Equipment by",
        "pie_empresa": "Los Amigos Energy · Solar Energy Lat, S.A. de C.V.",
        "pie_privado": "This page is private. Please don't share it outside your project.",

        "reporte_pdf": "Download PDF report",
        "reporte_titulo": "Project report",
        "reporte_generado": "Generated on {fecha}",
        "reporte_pie": "This document is an automatically generated backup of the online portal. "
                       "Data reflects the project's state at the time it was generated.",
    },
}


# ============================================================ /mi (nuevo)

# CLIENTES mapea token secreto -> config unificada del cliente. Ruta /mi/<token>.
# Cada cliente puede traer board_id (monday), vrm_id + banco_kwh (VRM), o ambos.
# "sistema", "documentos", "seriales" y "saldo" son config manual, igual que "sistema" en PORTALES.
# Ejemplo del .env:
#   CLIENTES={"tok...": {"nombre": "Casa Juan Pablo", "cliente": "Juan Pablo",
#     "ubicacion": "Tulum, Q. Roo", "board_id": "18423973736",
#     "vrm_id": 901035, "banco_kwh": 30.72, "soc_minimo": 10,
#     "sistema": {...},
#     "documentos": [{"tipo": "Contrato", "nombre": "Contrato de instalación", "url": "https://..."}],
#     "seriales": [{"equipo": "Inversor Victron Quattro 10kVA", "modelo": "...",
#                   "serie": "...", "garantia_hasta": "2029-08-10"}],
#     "saldo": {"total": 450000, "pagado": 300000}}}
CLIENTES = json.loads(os.environ.get("CLIENTES", "{}"))


def orden_secciones(obra_abierta, energizado, tiene_cfe):
    """Las secciones que siempre existen no cambian; solo cambia cuál va primero."""
    secciones = []
    if energizado:
        secciones.append("autonomia")
        if tiene_cfe:
            secciones.append("cfe")
    if obra_abierta:
        secciones.append("avance")
    secciones += ["documentos", "seriales", "sistema", "bitacora"]
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

    tiene_cfe = energizado and "cfe_bolsa" in cfg and "cfe_ancla" in cfg
    c = None
    if tiene_cfe:
        try:
            c = datos_cfe(cfg)
        except Exception:
            app.logger.exception("Falló la lectura de CFE para /mi/%s", token)
            hit = _cache_cfe.get(cfg["vrm_id"])
            c = hit[1] if hit else None

    lang = cfg.get("idioma", "es")
    if lang not in TRADUCCIONES:
        lang = "es"

    return render_template(
        "mi.html",
        p=cfg,
        m=m,
        v=v,
        c=c,
        token=token,
        lang=lang,
        t=TRADUCCIONES[lang],
        secciones=orden_secciones(obra_abierta, energizado, tiene_cfe),
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


# --------------------------------------------------------- reporte PDF
# "El portal muere, el PDF no": una instantánea descargable que no depende
# de que monday/VRM sigan respondiendo. Se genera al vuelo, no se guarda.
#
# Las fotos de monday vienen a resolución de cámara (varios MB c/u); sin
# comprimir, un reporte con fotos pesaba +100MB y tardaba ~50s. Se redimensionan
# y se bajan en paralelo para que quede ligero y dentro del timeout de gunicorn.

REPORTE_FOTOS_MAX = 10
REPORTE_FOTO_ANCHO = 640
REPORTE_FOTO_CALIDAD = 60


def foto_base64(asset_id):
    url = resolver_foto(asset_id)
    if not url:
        return None
    r = requests.get(url, timeout=20)
    r.raise_for_status()

    img = Image.open(BytesIO(r.content))
    img = ImageOps.exif_transpose(img).convert("RGB")
    if img.width > REPORTE_FOTO_ANCHO:
        alto = round(img.height * REPORTE_FOTO_ANCHO / img.width)
        img = img.resize((REPORTE_FOTO_ANCHO, alto), Image.LANCZOS)

    salida = BytesIO()
    img.save(salida, format="JPEG", quality=REPORTE_FOTO_CALIDAD, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(salida.getvalue()).decode("ascii")


def _foto_o_none(foto):
    try:
        return foto_base64(foto["id"])
    except Exception:
        app.logger.exception("No se pudo incrustar la foto %s en el reporte", foto["id"])
        return None


def limpio_pdf(texto):
    """El equipo en sitio a veces usa emoji en monday (➡️, ✅...); la fuente del
    PDF no los tiene y salen como cajas rotas. Se quitan solo para el PDF —
    en la página web se ven bien y no se tocan."""
    if not texto:
        return texto
    return texto.encode("latin-1", errors="ignore").decode("latin-1")


app.jinja_env.filters["limpio_pdf"] = limpio_pdf


def construir_reporte(cfg, m, lang):
    bitacora = [dict(b, fotos=[]) for b in (m["bitacora"] if m else [])]

    # Selecciona por adelantado cuáles fotos entran en el tope, preservando
    # el orden de la bitácora, para poder bajarlas todas en paralelo.
    pendientes = []
    fotos_restantes = REPORTE_FOTOS_MAX
    for i, b in enumerate(m["bitacora"] if m else []):
        for foto in b.get("fotos") or []:
            if fotos_restantes <= 0:
                break
            pendientes.append((i, foto))
            fotos_restantes -= 1

    if pendientes:
        with ThreadPoolExecutor(max_workers=3) as pool:
            resultados = pool.map(lambda par: _foto_o_none(par[1]), pendientes)
        for (i, foto), data_uri in zip(pendientes, resultados):
            if data_uri:
                bitacora[i]["fotos"].append({"nombre": foto["nombre"], "src": data_uri})

    with open(os.path.join(app.root_path, "static", "logo-los-amigos.png"), "rb") as f:
        logo = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")

    return {
        "p": cfg,
        "m": m,
        "bitacora": bitacora,
        "t": TRADUCCIONES[lang],
        "lang": lang,
        "logo": logo,
        "fecha": datetime.now(TZ).strftime("%d/%m/%Y"),
    }


@app.route("/mi/<token>/reporte.pdf")
def mi_reporte(token):
    cfg = CLIENTES.get(token)
    if not cfg:
        abort(404)

    m = None
    if "board_id" in cfg:
        try:
            m = datos_monday(cfg["board_id"])
        except Exception:
            app.logger.exception("Falló la lectura de monday para el reporte de /mi/%s", token)
            hit = _cache_monday.get(cfg["board_id"])
            m = hit[1] if hit else None

    lang = cfg.get("idioma", "es")
    if lang not in TRADUCCIONES:
        lang = "es"

    html = render_template("reporte_pdf.html", **construir_reporte(cfg, m, lang))

    buffer = BytesIO()
    resultado = pisa.CreatePDF(html, dest=buffer)
    if resultado.err:
        app.logger.error("Error generando el PDF para /mi/%s", token)
        abort(500)

    nombre = (cfg.get("nombre") or "proyecto").replace(" ", "-")
    return Response(
        buffer.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="reporte-{nombre}.pdf"'},
    )


@app.route("/healthz")
def healthz():
    return {"ok": True, "portales": len(PORTALES), "sitios": len(SITIOS), "clientes": len(CLIENTES)}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
