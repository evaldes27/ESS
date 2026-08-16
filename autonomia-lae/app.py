import json
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, abort, jsonify, render_template

app = Flask(__name__)

VRM_URL = "https://vrmapi.victronenergy.com/v2"
VRM_TOKEN = os.environ["VRM_TOKEN"]
POLL_SEGUNDOS = int(os.environ.get("POLL_SEGUNDOS", "60"))
VENTANA_MINUTOS = int(os.environ.get("VENTANA_MINUTOS", "20"))
SITIOS = json.loads(os.environ["SITIOS"])

TZ = timezone(timedelta(hours=-5))

_cache = {}
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


def construir(cfg):
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


def datos(cfg):
    vrm_id = cfg["vrm_id"]
    ahora = time.time()
    hit = _cache.get(vrm_id)
    if hit and ahora - hit[0] < POLL_SEGUNDOS:
        return hit[1]
    frescos = construir(cfg)
    _cache[vrm_id] = (ahora, frescos)
    return frescos


@app.route("/a/<token>")
def pagina(token):
    cfg = SITIOS.get(token)
    if not cfg:
        abort(404)
    try:
        d = datos(cfg)
    except Exception:
        app.logger.exception("Fallo la lectura de VRM")
        hit = _cache.get(cfg["vrm_id"])
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
        return jsonify(datos(cfg))
    except Exception:
        hit = _cache.get(cfg["vrm_id"])
        if not hit:
            abort(503)
        return jsonify(hit[1])


@app.route("/healthz")
def healthz():
    return {"ok": True, "sitios": len(SITIOS)}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
