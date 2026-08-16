# Monitor de apagones — Tulum

Mapa público que muestra en tiempo real qué zonas de Tulum tienen energía de CFE,
usando los sistemas Victron de tus instalaciones como sensores (alarma "Grid lost"
vía VRM API).

## Requisitos

- Docker y Docker Compose en tu homelab (o Python 3.10+ si prefieres sin Docker)
- Un token de acceso del VRM Portal

## 1. Generar el token VRM

1. Entra a https://vrm.victronenergy.com
2. Preferences → Integrations → Access tokens → **Add token**
3. Dale un nombre (ej. "monitor-apagones") y copia el token (solo se muestra una vez)

## 2. Configurar

```bash
cp .env.example .env       # pega tu token en .env
```

Descubre los idSite de tus instalaciones:

```bash
docker compose run --rm monitor-luz python app.py --discover
```

Esto lista cada instalación con su `idSite` y los atributos de red detectados
(alarma Grid lost o voltaje de entrada). Verifica que cada sitio muestre al menos
uno de los dos — así confirmas que la detección funcionará.

Luego crea tu `zonas.json` basándote en `zonas.example.json`:

```json
{
  "TU_ID_SITE": { "zona": "Aldea Zamá", "lat": 20.1972, "lng": -87.4489 }
}
```

**Importante (privacidad):** usa coordenadas del centro de la zona/colonia,
nunca la ubicación real del cliente. Varias instalaciones pueden compartir
la misma zona — el mapa las agrupa.

## 3. Levantar

```bash
docker compose up -d --build
```

El mapa queda en `http://IP-DE-TU-HOMELAB:8000`

## 4. Exponerlo a internet (recomendado: Cloudflare Tunnel)

Para hacerlo público sin abrir puertos en tu router:

1. Compra/usa un dominio en Cloudflare (ej. `hayluz.tudominio.com`)
2. Instala `cloudflared` en el homelab y crea un túnel apuntando a `localhost:8000`

Esto evita exponer tu IP de casa y te da HTTPS gratis. La documentación está en
https://developers.cloudflare.com/cloudflare-tunnel/

## Endpoints

- `GET /` — el mapa público
- `GET /api/estado` — estado actual por zona (JSON)
- `GET /api/historial` — últimos 200 cambios de estado (para estadísticas)

## Cómo funciona la detección

Por cada instalación, cada 90 segundos (configurable con `POLL_SECONDS`):

1. Busca la alarma **"Grid lost"** del VE.Bus en los diagnósticos del VRM.
   `rawValue 0` = hay red; `>= 1` = se perdió la red.
2. Si no existe esa alarma, usa el **voltaje de entrada AC** como respaldo:
   más de 80 V = hay red.

Una zona se marca "sin luz" si **cualquiera** de sus sitios perdió la red
(los cortes de CFE suelen afectar el área completa).

Cada cambio de estado se guarda en SQLite (`data/historial.db`), lo que te
permite después sacar estadísticas: apagones por mes, duración promedio,
zonas más afectadas, etc.

## Notas para el homelab

- Pon el módem/ONT y el homelab en el circuito respaldado por baterías:
  el mapa importa precisamente cuando no hay CFE.
- Si el internet de casa se cae durante un apagón general, el mapa quedará
  inaccesible aunque el servidor siga vivo. Si eso se vuelve problema, el
  plan B es mover solo el frontend a Cloudflare Pages y dejar el API en casa.
