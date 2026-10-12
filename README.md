# Huellas sin contacto API

Captura de las huellas de **4 dedos (índice, medio, anular y meñique)** con la cámara del celular o
de la computadora, evaluación en tiempo real, auto‑captura, realce de crestas, calidad por dedo y
extracción de minucias. Hecho con FastAPI y solo componentes open source con licencia comercial.

## Ejecutar

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000      # http://localhost:8000
```

En el **celular** la cámara solo funciona con HTTPS:

```bash
./scripts/run_https.sh                          # https://<IP-de-tu-Mac>:8443
```

Pruebas: `.venv/bin/pytest -q`

## Cómo funciona

**Navegador (tiempo real, `app/static/app.js`)**
1. MediaPipe Hands (WASM/GPU, archivos locales en `static/vendor`) detecta los 21 puntos de la mano en cada cuadro.
2. Se calcula la región de cada yema (falange distal) y se evalúa: mano detectada, palma al frente,
   dedos extendidos, dentro del encuadre, distancia (ancho del dedo en px), separación, luz/reflejo,
   estabilidad y **enfoque por yema** (laplaciano normalizado).
3. El autofoco se dirige a las yemas (`pointsOfInterest`) y se fuerza re‑enfoque si sigue borroso
   (Android/Chrome). Hay linterna, zoom y enfoque manual cuando la cámara los soporta.
4. **Envío automático continuo:** en cuanto la mano está bien puesta (palma, 4 dedos, encuadre, y distancia,
   luz y enfoque al menos en amarillo) se envían tomas en segundo plano, **una sola petición a la vez**: entre
   envío y envío se junta ~0.35 s de cuadros y se manda el más nítido (o al instante si todo está en verde).
   Si la calidad no alcanza la “calidad objetivo”, se sigue intentando mientras el usuario acomoda la mano
   y se conserva la mejor; con errores de red espera 1, 2, 4… hasta 10 s para no saturar el servidor.

**Servidor (`POST /api/v1/huellas/captura`)**
1. Recorte rotado de cada yema, normalizado a ~500 ppi y espejado para ser compatible con huellas de contacto.
2. Segmentación del dedo, aplanado de iluminación + CLAHE, campo de orientación, periodo de crestas por FFT.
3. Banco de 16 filtros de Gabor orientados → binarización → esqueleto → minucias (terminaciones y bifurcaciones).
4. Calidad 0‑100 y nivel 1‑5 por dedo (coherencia, nitidez espectral, contraste, área útil, resolución).

Endpoint directo de demostración (desactivable con `HUELLAS_DEMO=0`). Parámetros (multipart): `imagen` (archivo), `landmarks` (JSON opcional; si no viene, el servidor
detecta la mano), `mano` (`auto|derecha|izquierda`), `incluir_imagenes` (bool).

## Servicio central de captura (varios proyectos, sin almacenamiento)

Cada proyecto **no** copia la cámara ni la evaluación: pide una sesión, manda al usuario a capturar y
recibe el resultado **con todas las imágenes** en una sola petición firmada. **El servicio no guarda nada**:
las sesiones viven en memoria y las huellas se descartan en cuanto el proyecto confirma que las guardó.

```
Backend del proyecto                 Servicio de huellas (memoria)            Usuario
1. POST /api/v1/sesiones   ───────►  crea sesión  ──► { id, url_puente }
2. redirige a url_puente   ─────────────────────────────────────────────────►  QR o cámara
                                     3. procesa, aprueba o reintenta
4. POST multipart firmado  ◄───────  evento + resultado + 22 imágenes
   guarda todo y responde 200 ────►  olvida las huellas
5.                                   "¡Huellas registradas!" y regresa ───────►  sigue su trámite
```

Lo único en disco es la configuración de proyectos (`config/proyectos.json`: API keys en hash y secretos
de firma). Si el servicio se reinicia, se pierden solo las capturas que estaban en curso.

### 1. Dar de alta un proyecto

```bash
.venv/bin/python scripts/proyectos.py crear "Créditos" \
    --webhook https://api.creditos.com/webhooks/huellas --dominios creditos.com
```

Queda en `config/proyectos.json` (se recarga solo, sin reiniciar). Devuelve `api_key` (solo se muestra
una vez) y `webhook_secreto`; guárdalos en el backend del proyecto.
`--dominios` limita a dónde puede regresar al usuario, a dónde se mandan avisos y quién puede incrustar la
captura en un iframe. Otros comandos: `listar`, `rotar-key <id>`, `desactivar <id>`.

### 2. Crear una sesión (backend del proyecto, nunca desde el navegador)

```bash
curl -X POST https://huellas.tuempresa.com/api/v1/sesiones \
  -H "X-API-Key: hk_..." -H "Content-Type: application/json" \
  -d '{"referencia_externa":"cliente-123","mano":"derecha","calidad_minima":65,
       "url_retorno":"https://creditos.com/registro/paso-4","metadata":{"tramite":"alta"}}'
```

Campos opcionales: `mano` (`auto|derecha|izquierda`), `calidad_minima` (20-95), `max_intentos` (1-20),
`webhook_url` (si no, la del proyecto; **obligatoria** en alguno de los dos, es el único canal del
resultado), `expira_minutos` (5-1440), `metadata` (≤ 4 KB, vuelve en el aviso).
Respuesta: `{ "id": "ses_…", "estado": "pendiente", "url_puente": "https://…/puente/<token>",
"url_captura": "https://…/captura/<token>", "expira": … }`.

### 3. Mandar al usuario a capturar

- **Recomendado — `url_puente`:** un solo enlace para cualquier dispositivo.
  - En **celular** abre la cámara directo.
  - En **computadora** muestra un **QR**; el usuario lo escanea con el celular, captura ahí, y la
    computadora avanza sola (“Celular conectado” → “Capturando…” → redirige a `url_retorno`, o envía
    `postMessage` si está en un iframe). El celular solo muestra “Regresa a tu computadora”.
  - También ofrece “Capturar aquí mismo” por si la computadora tiene buena cámara.
  - Si prefieres dibujar el QR en tu propia pantalla: `GET /api/v1/sesiones/{id}/qr.svg` (con `X-API-Key`)
    y espera la entrega en tu backend (o consulta el estado con `GET /api/v1/sesiones/{id}`).
- **Redirección directa a la cámara (`url_captura`):** `location.href = url_captura`. Al terminar vuelve a
  `url_retorno?sesion_id=…&estado=completada&aprobada=1`.
- **Iframe** (el dominio debe estar en `--dominios`):
  ```html
  <iframe src="URL_CAPTURA" allow="camera; fullscreen" style="width:100%;height:100vh;border:0"></iframe>
  <script>
    addEventListener("message", (e) => {
      if (e.origin === "https://huellas.tuempresa.com" && e.data?.tipo === "huellas:finalizada") {
        // e.data = { sesion_id, estado, aprobada } -> tu backend ya recibió y guardó el resultado
      }
    });
  </script>
  ```
- **App móvil:** abrir `url_captura` en un WebView con permiso de cámara.

El usuario ve la misma pantalla de captura (sin JSON ni opciones técnicas). La sesión termina sola cuando una
captura alcanza `calidad_minima` con los 4 dedos, al agotar `max_intentos` (queda `aprobada: false`) o con el
botón “Usar la mejor captura”. Los envíos automáticos de la página (`automatico=true`) solo cuentan como intento
si aprueban; los demás igual pueden quedar como la mejor captura (tope de 300 por sesión).

### 4. Recibir la entrega (webhook)

Un `POST` **multipart/form-data** a `webhook_url`:

| Campo | Contenido |
|---|---|
| `evento` | `evento.json` (application/json): evento, sesión y resultado; las imágenes van **por nombre** |
| `imagenes` | Un archivo por imagen: `captura.jpg`, `mano_anotada.jpg` y por dedo `indice_original.png`, `indice_contraste.png`, `indice_realce_gabor.png`, `indice_binaria.png`, `indice_minucias.png` (igual para medio, anular, meñique) |

Cabeceras: `X-Huellas-Evento`, `X-Huellas-Entrega` (id único, para ignorar duplicados) y
`X-Huellas-Firma: t=<unix>,v1=<HMAC-SHA256(webhook_secreto, "<t>." + cuerpo_crudo)>`.

```json
{
  "id": "evt_…", "evento": "sesion.completada", "creado": "2026-10-10T18:00:00+00:00",
  "datos": {
    "sesion": { "id": "ses_…", "referencia_externa": "cliente-123", "estado": "completada", "aprobada": true,
                "mejor_calidad": 82.4, "intentos": 2, "metadata": {"tramite": "alta"} },
    "resultado": {
      "calidad_global": { "puntuacion": 82.4, "nivel": 1, "dedos_validos": 4 },
      "imagenes": { "captura": "captura.jpg", "mano_anotada": "mano_anotada.jpg" },
      "dedos": [ { "dedo": "indice", "posicion_nist": 2, "calidad": { … }, "minucias": [ … ],
                   "imagenes": { "original": "indice_original.png", "realce_gabor": "indice_realce_gabor.png", … } } ]
    }
  }
}
```

**Responde 2xx solo después de guardar todo.** Es la confirmación: el usuario ve “¡Huellas registradas!” y el
servicio olvida las huellas. Si respondes error o no contestas (20 s), se reintenta 2 veces al momento; si
sigue fallando, el usuario ve “No se pudo registrar” con un botón **Reintentar envío** (mientras no venza el
enlace). Eventos: `sesion.completada` (con imágenes), `sesion.expirada` y `sesion.cancelada` (solo `evento`).

Verificar la firma — Python (FastAPI):
```python
import hmac, hashlib, time, json
from pathlib import Path

@app.post("/webhooks/huellas")
async def webhook(request: Request, x_huellas_firma: str = Header("")):
    cuerpo = await request.body()                       # cuerpo crudo, antes de leer el formulario
    partes = dict(p.split("=", 1) for p in x_huellas_firma.split(","))
    esperado = hmac.new(SECRETO.encode(), f"{partes['t']}.".encode() + cuerpo, hashlib.sha256).hexdigest()
    if abs(time.time() - int(partes["t"])) > 300 or not hmac.compare_digest(esperado, partes["v1"]):
        raise HTTPException(401)
    form = await request.form()
    evento = json.loads(await form["evento"].read())
    for archivo in form.getlist("imagenes"):
        guardar(Path(archivo.filename).name, await archivo.read())   # S3, MinIO, disco…
    guardar_en_bd(evento["datos"])
    return {"ok": True}                                 # 200 = "ya lo guardé"
```

Node / Nuxt (`server/api/webhooks/huellas.post.ts`):
```ts
import { createHmac, timingSafeEqual } from "node:crypto";
export default defineEventHandler(async (event) => {
  const cuerpo = (await readRawBody(event, false)) as Buffer;          // cuerpo crudo
  const partes = Object.fromEntries(getHeader(event, "x-huellas-firma")!.split(",").map((p) => p.split("=")));
  const esperado = createHmac("sha256", process.env.HUELLAS_WEBHOOK_SECRETO!)
    .update(`${partes.t}.`).update(cuerpo).digest("hex");
  const a = Buffer.from(esperado), b = Buffer.from(partes.v1 ?? "");
  const ok = Math.abs(Date.now() / 1000 - Number(partes.t)) < 300 && a.length === b.length && timingSafeEqual(a, b);
  if (!ok) throw createError({ statusCode: 401 });
  const form = await readMultipartFormData(event);                     // [{ name, filename, data, type }]
  const evento = JSON.parse(form!.find((p) => p.name === "evento")!.data.toString());
  for (const img of form!.filter((p) => p.name === "imagenes")) {
    // guardar img.filename / img.data en tu almacenamiento
  }
  // guardar evento.datos en tu BD
  return { ok: true };
});
```

### 5. Otras rutas para el backend del proyecto

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/api/v1/sesiones/{id}` | Estado de la sesión y de la entrega (sin huellas) |
| GET | `/api/v1/sesiones` | Sesiones del proyecto que siguen en memoria (`?estado=pendiente`) |
| POST | `/api/v1/sesiones/{id}/cancelar` | Cancelar una sesión activa |
| GET | `/api/v1/sesiones/{id}/qr.svg` | QR para dibujarlo en tu propia pantalla |

Todas llevan `X-API-Key` y solo ven las sesiones de su proyecto. El estado de una sesión terminada se
recuerda 60 min (sin huellas) y luego se olvida (404).

### Ejemplo funcionando

[`ejemplos/proyecto_demo.py`](ejemplos/proyecto_demo.py) es un proyecto cliente completo (crear sesión,
verificar firma, guardar imágenes y resultado). Para probarlo desde el celular:

```bash
./scripts/demo_celular.sh
```

### Variables de entorno del servicio

| Variable | Default | Uso |
|---|---|---|
| `HUELLAS_URL_PUBLICA` | URL de la petición | URL pública de los enlaces de captura. **Defínela en producción.** |
| `HUELLAS_PROYECTOS` | `./config/proyectos.json` | Configuración de proyectos (no hay base de datos) |
| `HUELLAS_SESION_MINUTOS` | 30 | Tiempo para completar la captura |
| `HUELLAS_RECORDAR_ESTADO_MINUTOS` | 60 | Cuánto se recuerda el estado (sin huellas) de una sesión terminada |
| `HUELLAS_MAX_INTENTOS` / `HUELLAS_CALIDAD_MINIMA` | 6 / 65 (“muy buena”) | Valores por defecto de cada sesión |
| `HUELLAS_ENTREGA_TIMEOUT` / `HUELLAS_ENTREGA_REINTENTOS` | 20 s / 2 | Espera y reintentos inmediatos de la entrega |
| `HUELLAS_DEMO` | 1 | `0` desactiva la página `/` y `/api/v1/huellas/captura` (recomendado en producción) |
| `HUELLAS_PERMITIR_HTTP` | 0 | `1` acepta URLs `http://` (solo desarrollo) |

### Notas de producción

- Corre **una sola instancia** (las sesiones están en memoria). Para varias réplicas, las sesiones irían a
  Redis (sigue siendo temporal) o se usa afinidad de sesión en el balanceador.
- Pon el servicio detrás de HTTPS con certificado real y limita peticiones por IP en el proxy (nginx/Traefik).
- Respalda solo `config/proyectos.json`. No hay huellas que respaldar: cada proyecto es dueño de las suyas.
- Las huellas son datos personales sensibles (LFPDPPP): el aviso de privacidad, el consentimiento y la
  conservación los gestiona cada proyecto; este servicio no las almacena.

## Licencias (todas permiten uso comercial)

| Componente | Uso | Licencia |
|---|---|---|
| FastAPI, Uvicorn, Starlette | API | MIT / BSD |
| MediaPipe Hands (Python y JS) + modelo | Detección de mano | Apache 2.0 |
| OpenCV | Procesamiento de imagen | Apache 2.0 |
| NumPy, SciPy, scikit‑image | Cálculo, esqueletizado | BSD |
| Algoritmos de realce/minucias | Código propio de este repo | — |

Opcionales para certificación: **NIST NBIS** (mindtct/bozorth3, dominio público), **NFIQ 2** (NIST, dominio público),
**SourceAFIS** (Apache 2.0) para comparar huellas 1:1 / 1:N.

## Limitaciones honestas

- La calidad depende sobre todo de la cámara: cámaras traseras de celular con linterna dan los mejores
  resultados; muchas webcams de laptop son de foco fijo y no resuelven crestas de cerca.
- La calidad 1‑5 es una métrica propia, **no** es NFIQ 2 oficial. Para trámites o interoperar con un
  AFIS de gobierno hay que validar con NFIQ 2 y exportar en ISO/IEC 19794‑4 / WSQ / ANSI‑NIST.
- iOS Safari no permite controlar enfoque, linterna ni zoom desde la web; funciona, pero con el
  autofoco del sistema.
- Productos como ONYX (Telos) llevan años de calibración con datos reales; este proyecto es una base
  funcional que hay que ajustar (umbrales en “Avanzado”) con capturas reales de tu población objetivo.
