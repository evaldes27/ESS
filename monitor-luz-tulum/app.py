"""
Monitor de apagones — Tulum
Consulta el VRM API de Victron, detecta "Grid lost" por instalación,
agrupa por zona y expone el estado como JSON para el mapa público.

Uso:
  python app.py --discover   # lista tus instalaciones y atributos de red (para armar zonas.json)
  python app.py              # corre el monitor + servidor web
"""

import os
import json
import time
import sqlite3
import argparse
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, send_from_directory, request

# ---------- Configuración ----------
VRM_API = "https://vrmapi.victronenergy.com/v2"
VRM_TOKEN = os.environ.get("VRM_TOKEN", "")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "90"))
DB_PATH = os.environ.get("DB_PATH", "data/historial.db")
ZONAS_FILE = os.environ.get("ZONAS_FILE", "zonas.json")
PUERTO = int(os.environ.get("PORT", "8000"))

HEADERS = {"X-Authorization": f"Token {VRM_TOKEN}"}

# Estado en memoria: idSite -> dict
estado_sitios = {}
lock = threading.Lock()


# ---------- VRM API ----------
def vrm_get(path, params=None):
    r = requests.get(f"{VRM_API}{path}", headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def obtener_user_id():
    return vrm_get("/users/me")["user"]["id"]


def listar_instalaciones(user_id):
    data = vrm_get(f"/users/{user_id}/installations")
    return data.get("records", [])


def diagnostico_sitio(id_site):
    data = vrm_get(f"/installations/{id_site}/diagnostics", params={"count": 1000})
    return data.get("records", [])


import re

V_MIN = float(os.environ.get("V_MIN", "113"))
V_MAX = float(os.environ.get("V_MAX", "135"))


def estado_red(diagnosticos):
    """
    Devuelve una tupla (estado, voltajes) donde:
      estado:  "ok" | "malo" | "sin_red" | None
        "ok"      -> hay red y todas las fases dentro de 113–135 V
        "malo"    -> hay red pero alguna fase fuera de rango (brownout/sobretensión)
        "sin_red" -> apagón (alarma de red activa)
        None      -> no se pudo determinar
      voltajes: dict {"L1": 130.1, "L2": 129.8, ...} con las fases que reportan voltaje.

    La alarma de red manda. Si la alarma dice que hay red, los voltajes de
    fase distinguen entre ok y malo.
    """
    alarma = None  # True = hay alarma (sin red); False = sin alarma (hay red)
    voltajes = {}

    for attr in diagnosticos:
        desc = (attr.get("description") or "").lower()
        raw = attr.get("rawValue")

        es_alarma_red = ("grid lost" in desc) or (desc.strip() == "grid alarm")
        if es_alarma_red:
            try:
                alarma = float(raw) >= 1
            except (TypeError, ValueError):
                pass

        if "input voltage" in desc:
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            # detectar número de fase: "input voltage phase 1" -> L1
            m = re.search(r"phase\s*(\d)", desc)
            etiqueta = f"L{m.group(1)}" if m else f"L{len(voltajes)+1}"
            voltajes[etiqueta] = round(v, 1)

    # Solo fases con voltaje real (ignora fases no usadas que reportan ~0 V)
    reales = {k: v for k, v in voltajes.items() if v > 20}

    # 1) Alarma activa -> apagón
    if alarma is True:
        if reales:
            return "malo", reales
        return "sin_red", reales

    # 2) Hay red: evaluar calidad por fase
    if alarma is False or reales:
        if reales:
            if any(v < V_MIN or v > V_MAX for v in reales.values()):
                return "malo", reales
            return "ok", reales
        if alarma is False:
            return "ok", {}

    return None, reales


# ---------- Historial (SQLite) ----------
def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_site TEXT NOT NULL,
            zona TEXT,
            con_luz INTEGER NOT NULL,
            estado TEXT,
            ts TEXT NOT NULL
        )
    """)
    # Migración suave: si la BD ya existía sin la columna 'estado', agregarla.
    cols = [r[1] for r in con.execute("PRAGMA table_info(eventos)").fetchall()]
    if "estado" not in cols:
        con.execute("ALTER TABLE eventos ADD COLUMN estado TEXT")
    con.commit()
    con.close()


def registrar_evento(id_site, zona, con_luz, estado=None):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO eventos (id_site, zona, con_luz, estado, ts) VALUES (?, ?, ?, ?, ?)",
        (str(id_site), zona, int(con_luz), estado,
         datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()


def ultimo_cambio(id_site):
    con = sqlite3.connect(DB_PATH)
    fila = con.execute(
        "SELECT ts FROM eventos WHERE id_site = ? ORDER BY id DESC LIMIT 1",
        (str(id_site),),
    ).fetchone()
    con.close()
    return fila[0] if fila else None


# ---------- Monitoreo ----------
def cargar_zonas():
    with open(ZONAS_FILE, encoding="utf-8") as f:
        return json.load(f)


def ciclo_monitoreo():
    zonas_cfg = cargar_zonas()
    user_id = obtener_user_id()

    while True:
        try:
            instalaciones = listar_instalaciones(user_id)
            ahora = datetime.now(timezone.utc).isoformat()

            for inst in instalaciones:
                id_site = str(inst.get("idSite"))
                if id_site not in zonas_cfg:
                    continue  # instalación no mapeada a zona, se ignora

                try:
                    diags = diagnostico_sitio(id_site)
                    estado, voltajes = estado_red(diags)  # ("ok"|"malo"|"sin_red"|None, {"L1":..})
                except Exception as e:
                    print(f"[{id_site}] error de consulta: {e}")
                    estado, voltajes = None, {}

                with lock:
                    anterior = estado_sitios.get(id_site, {}).get("estado")
                    if estado is not None and estado != anterior:
                        # con_luz se mantiene para compatibilidad con el historial:
                        # solo "sin_red" cuenta como apagón (0); ok y malo = hay luz (1)
                        con_luz = 0 if estado == "sin_red" else 1
                        registrar_evento(id_site, zonas_cfg[id_site]["zona"], con_luz, estado)
                        etiqueta = {"ok": "CON luz", "malo": "VOLTAJE fuera de rango",
                                    "sin_red": "SIN luz"}.get(estado, estado)
                        vtxt = " ".join(f"{k}:{v}V" for k, v in voltajes.items())
                        print(f"[{id_site}] {zonas_cfg[id_site]['zona']}: {etiqueta} {vtxt}")
                    estado_sitios[id_site] = {
                        "estado": estado,
                        "voltajes": voltajes,
                        "zona": zonas_cfg[id_site]["zona"],
                        "ultima_consulta": ahora,
                    }
        except Exception as e:
            print(f"Error en ciclo de monitoreo: {e}")

        time.sleep(POLL_SECONDS)


# ---------- API web ----------
app = Flask(__name__, static_folder="static")


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/estado")
def api_estado():
    zonas_cfg = cargar_zonas()
    # Cargar directorio de empresas
    empresas_file = os.path.join(os.path.dirname(ZONAS_FILE), "empresas.json")
    try:
        with open(empresas_file, encoding="utf-8") as f:
            empresas = json.load(f)
    except Exception:
        empresas = {}

    agrupado = {}

    with lock:
        snapshot = dict(estado_sitios)

    for id_site, cfg in zonas_cfg.items():
        z = cfg["zona"]
        instalador_id = cfg.get("instalador", "")
        if z not in agrupado:
            agrupado[z] = {
                "zona": z,
                "lat": cfg["lat"],
                "lng": cfg["lng"],
                "sitios": 0,
                "sin_red": 0,
                "malo": 0,
                "desde": None,
                "voltajes": {},
                "instalador": instalador_id,
                "_volt_prio": -1,
            }
        agrupado[z]["sitios"] += 1

        st = snapshot.get(id_site)
        estado_sitio = st.get("estado") if st else None

        if estado_sitio == "sin_red":
            agrupado[z]["sin_red"] += 1
            cambio = ultimo_cambio(id_site)
            if cambio and (agrupado[z]["desde"] is None or cambio < agrupado[z]["desde"]):
                agrupado[z]["desde"] = cambio
        elif estado_sitio == "malo":
            agrupado[z]["malo"] += 1

        if st and st.get("voltajes"):
            prio = {"malo": 2, "ok": 1}.get(estado_sitio, 0)
            if prio > agrupado[z]["_volt_prio"]:
                agrupado[z]["_volt_prio"] = prio
                agrupado[z]["voltajes"] = st["voltajes"]

    zonas = []
    for z in agrupado.values():
        if z["sin_red"] > 0:
            z["estado"] = "sin_red"
        elif z["malo"] > 0:
            z["estado"] = "malo"
        else:
            z["estado"] = "ok"
        z["con_luz"] = z["estado"] != "sin_red"
        z.pop("_volt_prio", None)
        if z["estado"] == "sin_red":
            z["voltajes"] = {}
        zonas.append(z)

    return jsonify({
        "actualizado": datetime.now(timezone.utc).isoformat(),
        "zonas": zonas,
        "empresas": empresas,
    })


@app.get("/api/historial")
def api_historial():
    con = sqlite3.connect(DB_PATH)
    filas = con.execute(
        "SELECT zona, con_luz, estado, ts FROM eventos ORDER BY id DESC LIMIT 200"
    ).fetchall()
    con.close()
    return jsonify([
        {"zona": f[0], "con_luz": bool(f[1]), "estado": f[2], "ts": f[3]} for f in filas
    ])


@app.get("/api/sitios")
def api_sitios():
    """
    Un marcador por instalación individual.
    Cada sitio usa la coordenada aproximada de su zona (privacidad)
    más un pequeño desplazamiento aleatorio fijo para que no se apilen exactamente.
    """
    import hashlib
    zonas_cfg = cargar_zonas()
    with lock:
        snapshot = dict(estado_sitios)

    sitios = []
    for id_site, cfg in zonas_cfg.items():
        st = snapshot.get(id_site, {})
        estado = st.get("estado") or "desconocido"
        voltajes = st.get("voltajes", {})

        # Desplazamiento pseudoaleatorio fijo por id_site (reproducible, ±~300m)
        h = int(hashlib.md5(id_site.encode()).hexdigest()[:8], 16)
        dlat = ((h & 0xFF) - 128) * 0.0025 / 128
        dlng = (((h >> 8) & 0xFF) - 128) * 0.0030 / 128

        sitios.append({
            "id": id_site,
            "zona": cfg["zona"],
            "lat": round(cfg["lat"] + dlat, 6),
            "lng": round(cfg["lng"] + dlng, 6),
            "estado": estado,
            "voltajes": voltajes,
            "instalador": cfg.get("instalador", ""),
        })

    return jsonify({
        "actualizado": datetime.now(timezone.utc).isoformat(),
        "sitios": sitios,
    })


@app.get("/api/stats")
def api_stats():
    """
    Estadísticas de apagones y voltaje por zona.
    Query param: periodo = 7d | 30d | 90d | 180d | all (default: all)
    Devuelve:
      - resumen global (total_apagones, horas_sin_luz, zonas_afectadas,
                        total_alertas_voltaje)
      - por_zona: lista con stats por zona
      - histograma_diario: conteo de eventos por día (últimos 30d)
    """
    periodo = request.args.get("periodo", "all")
    dias = {"7d": 7, "30d": 30, "90d": 90, "180d": 180}.get(periodo)

    con = sqlite3.connect(DB_PATH)

    where = ""
    if dias:
        desde = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        from datetime import timedelta
        desde -= timedelta(days=dias - 1)
        where = f"WHERE ts >= '{desde.isoformat()}'"

    filas = con.execute(
        f"SELECT id_site, zona, con_luz, estado, ts FROM eventos {where} ORDER BY ts ASC"
    ).fetchall()
    con.close()

    # ---- Construir apagones emparejando caídas y regresos ----
    # Guardamos el último evento por zona para calcular duraciones
    ultimo = {}   # zona -> (estado, ts)
    apagones = [] # {zona, inicio, fin, duracion_min}
    alertas_volt = 0

    for _, zona, con_luz, estado, ts in filas:
        prev = ultimo.get(zona)
        if prev:
            prev_estado, prev_ts = prev
            # Cerrar apagón previo
            if prev_estado == "sin_red" and estado != "sin_red":
                t0 = datetime.fromisoformat(prev_ts.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                dur = round((t1 - t0).total_seconds() / 60)
                apagones.append({"zona": zona, "inicio": prev_ts,
                                 "fin": ts, "duracion_min": dur})
        if estado == "malo":
            alertas_volt += 1
        ultimo[zona] = (estado, ts)

    # Apagones aún abiertos
    ahora = datetime.now(timezone.utc).isoformat()
    for zona, (est, ts) in ultimo.items():
        if est == "sin_red":
            t0 = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            t1 = datetime.now(timezone.utc)
            dur = round((t1 - t0).total_seconds() / 60)
            apagones.append({"zona": zona, "inicio": ts,
                             "fin": None, "duracion_min": dur})

    # ---- Stats por zona ----
    from collections import defaultdict
    zona_stats = defaultdict(lambda: {
        "apagones": 0, "minutos_sin_luz": 0,
        "apagon_max_min": 0, "alertas_voltaje": 0
    })
    for a in apagones:
        z = zona_stats[a["zona"]]
        z["apagones"] += 1
        z["minutos_sin_luz"] += a["duracion_min"]
        if a["duracion_min"] > z["apagon_max_min"]:
            z["apagon_max_min"] = a["duracion_min"]

    for _, zona, _, estado, _ in filas:
        if estado == "malo":
            zona_stats[zona]["alertas_voltaje"] += 1

    por_zona = []
    for zona, s in sorted(zona_stats.items(),
                          key=lambda x: -x[1]["minutos_sin_luz"]):
        por_zona.append({
            "zona": zona,
            "apagones": s["apagones"],
            "horas_sin_luz": round(s["minutos_sin_luz"] / 60, 1),
            "apagon_max_min": s["apagon_max_min"],
            "alertas_voltaje": s["alertas_voltaje"],
        })

    # ---- Histograma diario (últimos 30 días) ----
    from collections import Counter
    histo = Counter()
    for a in apagones:
        dia = a["inicio"][:10]
        histo[dia] += 1
    histograma = [{"dia": k, "apagones": v}
                  for k, v in sorted(histo.items())[-30:]]

    total_min = sum(a["duracion_min"] for a in apagones)
    zonas_afectadas = len({a["zona"] for a in apagones})

    return jsonify({
        "periodo": periodo,
        "resumen": {
            "total_apagones": len(apagones),
            "horas_sin_luz": round(total_min / 60, 1),
            "zonas_afectadas": zonas_afectadas,
            "alertas_voltaje": alertas_volt,
        },
        "por_zona": por_zona,
        "histograma": histograma,
        "generado": ahora,
    })


# ---------- Modo descubrimiento ----------
def descubrir():
    user_id = obtener_user_id()
    instalaciones = listar_instalaciones(user_id)
    print(f"\n{len(instalaciones)} instalaciones encontradas:\n")
    for inst in instalaciones:
        id_site = inst.get("idSite")
        print(f"  idSite: {id_site}  —  {inst.get('name')}")
        try:
            diags = diagnostico_sitio(id_site)
            relevantes = [
                a for a in diags
                if "grid" in (a.get("description") or "").lower()
                or "input voltage" in (a.get("description") or "").lower()
            ]
            for a in relevantes:
                print(f"      · {a.get('description')}: "
                      f"{a.get('formattedValue')} (raw: {a.get('rawValue')})")
        except Exception as e:
            print(f"      (error consultando diagnósticos: {e})")
    print("\nUsa estos idSite para armar tu zonas.json (ver zonas.example.json).\n")



from cfe_estimate import estimator_bp
app.register_blueprint(estimator_bp)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--discover", action="store_true",
                        help="Lista instalaciones y atributos de red, sin levantar el servidor")
    args = parser.parse_args()

    if not VRM_TOKEN:
        raise SystemExit("Falta la variable de entorno VRM_TOKEN (ver README).")

    if args.discover:
        descubrir()
    else:
        init_db()
        threading.Thread(target=ciclo_monitoreo, daemon=True).start()
        app.run(host="0.0.0.0", port=PUERTO)
