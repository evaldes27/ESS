import os, json, time, calendar, datetime as dt
import requests
from flask import Blueprint, jsonify, request, Response, abort

VRM_TOKEN = os.environ.get("VRM_TOKEN", "")
VRM_BASE  = "https://vrmapi.victronenergy.com/v2"

CLIENTS = {
    "m4k9p2xq7bf3": {"site_id": 822793, "bolsa": 235.0,
                     "name": "Alan Doyle", "house": "Lluvia 09"},
}

ANCHOR_READ_DATE = dt.date(2026, 6, 22)
CYCLE_MONTHS     = 2
BILLING_MODE     = "net"
GRID_IMPORT_KEYS = ["Gc", "Gb"]
GRID_EXPORT_KEYS = ["Pg", "Bg"]
CALIBRATION      = 1.0
CACHE_TTL        = 600
GRACE_DAYS       = 4

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "static", "cfe_1d_estimador.html")

estimator_bp = Blueprint("estimator", __name__)
_cache = {}

def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))

def current_period(today):
    start = ANCHOR_READ_DATE
    nxt = add_months(start, CYCLE_MONTHS)
    while nxt <= today:
        start, nxt = nxt, add_months(nxt, CYCLE_MONTHS)
    return start, nxt

def vrm_kwh_stats(site_id, start_date, end_date):
    params = {"type": "kwh",
              "start": int(time.mktime(start_date.timetuple())),
              "end":   int(time.mktime(end_date.timetuple())),
              "interval": "days"}
    headers = {"X-Authorization": f"Token {VRM_TOKEN}"}
    r = requests.get(f"{VRM_BASE}/installations/{site_id}/stats",
                     params=params, headers=headers, timeout=25)
    r.raise_for_status()
    return r.json()

def sum_key(records, key):
    total = 0.0
    for pt in (records.get(key) or []):
        try:
            if pt[1] is not None:
                total += float(pt[1])
        except (TypeError, IndexError, ValueError):
            pass
    return total

def sum_keys(records, keys):
    return sum(sum_key(records, k) for k in keys)

def compute_estimate(site_id, bolsa):
    today = dt.date.today()
    start, end = current_period(today)
    days_elapsed = max((today - start).days, 1)
    days_total   = max((end - start).days, days_elapsed)
    season = "verano" if today.month in (5, 6, 7, 8, 9, 10) else "invierno"

    if days_elapsed <= GRACE_DAYS:
        return {
            "billing_mode": BILLING_MODE,
            "import_kwh": 0, "export_kwh": 0, "net_projected": 0,
            "bolsa_balance": bolsa, "bolsa_applied": 0, "bolsa_after": bolsa,
            "billed_kwh": 0, "period_just_started": True,
            "season_hint": season,
            "period": {"start": start.isoformat(), "end": end.isoformat(),
                       "days_elapsed": days_elapsed, "days_total": days_total},
        }

    payload = vrm_kwh_stats(site_id, start, today + dt.timedelta(days=1))
    records = payload.get("records") or {}
    imp = sum_keys(records, GRID_IMPORT_KEYS) * CALIBRATION
    exp = sum_keys(records, GRID_EXPORT_KEYS) * CALIBRATION
    net_projected = (imp - exp) / days_elapsed * days_total
    if net_projected >= 0:
        bolsa_applied = min(bolsa, net_projected)
        bolsa_after   = max(0.0, bolsa - net_projected)
    else:
        bolsa_applied = 0.0
        bolsa_after   = bolsa + (-net_projected)
    billed_kwh = max(0.0, net_projected - bolsa)

    return {
        "billing_mode": BILLING_MODE,
        "import_kwh": round(imp, 1), "export_kwh": round(exp, 1),
        "net_projected": round(net_projected, 1),
        "bolsa_balance": round(bolsa, 1),
        "bolsa_applied": round(bolsa_applied, 1),
        "bolsa_after": round(bolsa_after, 1),
        "billed_kwh": round(billed_kwh, 1),
        "season_hint": season,
        "period": {"start": start.isoformat(), "end": end.isoformat(),
                   "days_elapsed": days_elapsed, "days_total": days_total},
    }

@estimator_bp.route("/api/recibo/<code>")
def api_recibo(code):
    client = CLIENTS.get(code)
    if not client:
        abort(404)
    if not VRM_TOKEN:
        return jsonify({"error": "VRM_TOKEN no configurado"}), 200
    if request.args.get("debug") == "1":
        try:
            today = dt.date.today()
            start, _ = current_period(today)
            payload = vrm_kwh_stats(client["site_id"], start, today + dt.timedelta(days=1))
            keys = {k: round(sum_key(payload["records"], k), 2)
                    for k, arr in (payload.get("records") or {}).items()
                    if isinstance(arr, list)}
            return jsonify({"available_keys": keys})
        except Exception as e:
            return jsonify({"error": str(e)}), 200
    hit = _cache.get(code)
    if hit and (time.time() - hit[0]) < CACHE_TTL:
        return jsonify(hit[1])
    try:
        data = compute_estimate(client["site_id"], client["bolsa"])
    except Exception as e:
        return jsonify({"error": "VRM: " + str(e)[:160]}), 200
    _cache[code] = (time.time(), data)
    return jsonify(data)

@estimator_bp.route("/recibo/<code>")
def page_recibo(code):
    if code not in CLIENTS:
        abort(404)
    client = CLIENTS[code]
    try:
        with open(HTML_PATH, encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        return Response("No encuentro la pagina del estimador", status=500)
    inject = ("<script>"
              f"window.CODE={json.dumps(code)};"
              f"window.CLIENT_NAME={json.dumps(client.get('name',''))};"
              f"window.CLIENT_HOUSE={json.dumps(client.get('house',''))};"
              "window.API_BASE='';</script>")
    html = html.replace("</head>", inject + "</head>", 1)
    return Response(html, mimetype="text/html")

if __name__ == "__main__":
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(estimator_bp)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8095")))
