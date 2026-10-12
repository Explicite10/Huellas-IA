"""Proyecto cliente de ejemplo: así se integra cualquier backend con el servicio de huellas.

1. POST /iniciar          -> crea la sesión en el servicio y manda al usuario a la captura
2. POST /webhooks/huellas -> recibe el aviso firmado CON las imágenes y las guarda "en su casa";
                             solo cuando responde 200 el usuario ve "¡Huellas registradas!"
3. GET  /registro/fin     -> página a la que regresa el usuario

Ejecutar (con el servicio corriendo en :8443):
    HUELLAS_URL=https://localhost:8443 HUELLAS_API_KEY=hk_... HUELLAS_WEBHOOK_SECRETO=whsec_... \\
    HUELLAS_VERIFICAR_TLS=0 .venv/bin/uvicorn ejemplos.proyecto_demo:app --port 8100
"""
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

SERVICIO = os.getenv("HUELLAS_URL", "https://localhost:8443").rstrip("/")
API_KEY = os.getenv("HUELLAS_API_KEY", "")
SECRETO = os.getenv("HUELLAS_WEBHOOK_SECRETO", "")
VERIFICAR_TLS = os.getenv("HUELLAS_VERIFICAR_TLS", "1") == "1"   # 0 solo con el certificado autofirmado local
URL_PROPIA = os.getenv("PROYECTO_URL", "http://localhost:8100").rstrip("/")
DATOS = Path(__file__).parent / "datos_proyecto"   # aquí iría tu S3 / BD
DATOS.mkdir(exist_ok=True)

app = FastAPI(title="Proyecto de ejemplo (cliente del servicio de huellas)")
app.mount("/archivos", StaticFiles(directory=DATOS), name="archivos")


def cliente_servicio() -> httpx.Client:
    return httpx.Client(base_url=SERVICIO, headers={"X-API-Key": API_KEY}, verify=VERIFICAR_TLS, timeout=30)


ESTILOS = """
:root{--fondo:#f4f6f8;--panel:#fff;--texto:#18212b;--tenue:#5d6b79;--borde:#dde3ea;--acento:#0f6fde;--ok:#1a9b53;--medio:#d98a00;--mal:#d43c3c}
*{box-sizing:border-box}
body{font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;max-width:1040px;margin:0 auto;padding:16px;background:var(--fondo);color:var(--texto)}
h1{font-size:1.35rem;margin:4px 0 12px}h2{font-size:1rem;margin:0 0 10px}
.c{background:var(--panel);border:1px solid var(--borde);border-radius:12px;padding:14px;margin:0 0 12px}
button,input{font:inherit;padding:10px 14px;border-radius:8px;border:1px solid #ccd}button{background:var(--acento);color:#fff;border:0;cursor:pointer}
a{color:var(--acento)}pre{white-space:pre-wrap;word-break:break-all;font-size:12px;margin:8px 0 0}code{word-break:break-all}
img{display:block;max-width:100%}
.resumen{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px}
.insignia{padding:4px 12px;border-radius:999px;color:#fff;font-weight:600;font-size:.9rem;white-space:nowrap}
.insignia.ok{background:var(--ok)}.insignia.medio{background:var(--medio)}.insignia.mal{background:var(--mal)}
.datos{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;flex:1;min-width:240px}
.datos div{border:1px solid var(--borde);border-radius:10px;padding:6px 10px}
.datos span{display:block;font-size:.72rem;color:var(--tenue)}.datos b{font-variant-numeric:tabular-nums;word-break:break-all}
.fotos{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.fotos img{width:100%;height:clamp(190px,30vw,300px);object-fit:contain;background:#1b232c;border-radius:8px}
.dedos{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.dedo{margin:0}
.dedo-cab{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:4px 8px;margin-bottom:8px}
.dedo-cab small{color:var(--tenue)}
.etapas{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px}
.etapas img{width:100%;aspect-ratio:3/4;object-fit:contain;background:#fff;border:1px solid var(--borde);border-radius:6px}
figure{margin:0;text-align:center;cursor:zoom-in;min-width:0}
figcaption{font-size:11px;color:var(--tenue);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.visor{position:fixed;inset:0;background:rgba(0,0,0,.85);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;padding:16px;z-index:9;cursor:zoom-out}
.visor[hidden]{display:none}.visor img{max-width:100%;max-height:85vh;background:#fff;border-radius:8px}.visor p{color:#fff;margin:0}
@media(max-width:860px){.dedos{grid-template-columns:minmax(0,1fr)}}
@media(max-width:480px){body{padding:12px}.c{padding:12px}.etapas{gap:4px}figcaption{font-size:10px}}
"""


def pagina(cuerpo: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Proyecto demo</title>
<style>{ESTILOS}</style></head><body><h1>Proyecto demo · Registro</h1>{cuerpo}
<div class="visor" id="visor" hidden><img alt=""><p></p></div>
<script>
const v=document.getElementById("visor");
document.addEventListener("click",e=>{{const f=e.target.closest("figure");
 if(f){{v.querySelector("img").src=f.querySelector("img").src;v.querySelector("p").textContent=f.dataset.titulo||"";v.hidden=false}}
 else if(e.target.closest("#visor"))v.hidden=true}});
</script></body></html>""")


@app.get("/")
def inicio():
    return pagina("""<div class="c"><p>Paso 3 de 4: registra tus huellas.</p>
<form method="post" action="/iniciar"><input name="referencia" value="cliente-123">
<button>Registrar huellas</button></form></div>""")


@app.post("/iniciar")
def iniciar(referencia: str = Form(...)):
    with cliente_servicio() as c:
        r = c.post("/api/v1/sesiones", json={
            "referencia_externa": referencia,
            "mano": "auto",
            "url_retorno": f"{URL_PROPIA}/registro/fin",
            "webhook_url": f"{URL_PROPIA}/webhooks/huellas",
            "metadata": {"paso": 3},
        })
    if r.status_code != 201:
        raise HTTPException(502, f"El servicio de huellas respondió {r.status_code}: {r.text}")
    # url_puente: en computadora muestra un QR para seguir en el celular; en celular abre la cámara directo.
    return RedirectResponse(r.json()["url_puente"], status_code=303)


def firma_valida(cabecera: str, cuerpo: bytes) -> bool:
    try:
        partes = dict(p.split("=", 1) for p in cabecera.split(","))
        t = int(partes["t"])
    except (ValueError, KeyError):
        return False
    esperado = hmac.new(SECRETO.encode(), f"{t}.".encode() + cuerpo, hashlib.sha256).hexdigest()
    return abs(time.time() - t) < 300 and hmac.compare_digest(esperado, partes.get("v1", ""))


@app.post("/webhooks/huellas")
async def webhook(request: Request, x_huellas_firma: str = Header(""), x_huellas_entrega: str = Header("")):
    """Recibe evento + imágenes (multipart). Responder 200 SOLO después de guardar: el servicio no conserva nada."""
    cuerpo = await request.body()                      # la firma cubre el cuerpo crudo completo
    if not firma_valida(x_huellas_firma, cuerpo):
        raise HTTPException(401, "Firma inválida")
    marca = DATOS / f".{x_huellas_entrega}"
    if marca.exists():                                 # el mismo aviso puede llegar dos veces: se ignora
        return {"ok": True, "duplicado": True}
    formulario = await request.form()
    evento = json.loads(await formulario["evento"].read())
    carpeta = DATOS / evento["datos"]["sesion"]["id"]
    carpeta.mkdir(exist_ok=True)
    for archivo in formulario.getlist("imagenes"):     # captura.jpg, mano_anotada.jpg, indice_original.png…
        nombre = Path(archivo.filename).name           # nunca confiar en rutas recibidas
        (carpeta / nombre).write_bytes(await archivo.read())
    (carpeta / "resultado.json").write_text(json.dumps(evento["datos"], ensure_ascii=False, indent=2))
    marca.touch()
    return {"ok": True}


ETAPAS = [("original", "Original"), ("contraste", "Contraste"), ("realce_gabor", "Realce Gabor"),
          ("binaria", "Binaria"), ("minucias", "Minucias")]


@app.get("/registro/fin")
def fin(sesion_id: str, estado: str, aprobada: str = "0"):
    archivo = DATOS / sesion_id / "resultado.json"
    if not archivo.exists():
        return pagina(f"""<div class="c"><p>Estado: <b>{estado}</b>. Esperando el aviso del servicio…</p>
<script>setTimeout(()=>location.reload(),1500)</script></div>""")
    datos = json.loads(archivo.read_text())
    s, r = datos["sesion"], datos.get("resultado") or {}
    ruta = f"/archivos/{sesion_id}"

    def figura(nombre: str, titulo: str, completo: str) -> str:
        return (f'<figure data-titulo="{completo}"><img src="{ruta}/{nombre}" alt="{completo}" loading="lazy">'
                f'<figcaption>{titulo}</figcaption></figure>')

    nombres = {"captura": "Foto tomada", "mano_anotada": "Dedos detectados"}
    fotos = "".join(figura(n, nombres.get(k, k), nombres.get(k, k)) for k, n in r.get("imagenes", {}).items())
    dedos = ""
    for d in r.get("dedos", []):
        nombre = d["dedo"].replace("menique", "meñique").replace("indice", "índice").capitalize()
        if d.get("estado") != "ok":
            dedos += f'<div class="c dedo"><div class="dedo-cab"><b>{nombre}</b><small>{d["estado"]}</small></div></div>'
            continue
        q = d["calidad"]
        tono = "ok" if q["nivel"] <= 2 else "medio" if q["nivel"] <= 3 else "mal"
        dedos += (f'<div class="c dedo"><div class="dedo-cab"><b>{nombre} <small>· NIST {d["posicion_nist"]}</small></b>'
                  f'<span><span class="insignia {tono}">{q["puntuacion"]} · {q["etiqueta"]}</span> '
                  f'<small>{d["minucias_total"]} minucias</small></span></div><div class="etapas">'
                  + "".join(figura(d["imagenes"][k], t, f"{nombre} · {t}") for k, t in ETAPAS if k in d.get("imagenes", {}))
                  + "</div></div>")

    if s.get("aprobada"):
        insignia = '<span class="insignia ok">Huellas aprobadas</span>'
    elif s["estado"] == "completada":
        insignia = '<span class="insignia medio">Calidad insuficiente</span>'
    else:
        insignia = f'<span class="insignia mal">{s["estado"].capitalize()}</span>'
    g = r.get("calidad_global", {})
    resumen = "".join(f"<div><span>{k}</span><b>{v}</b></div>" for k, v in [
        ("Calidad", g.get("puntuacion", "—")), ("Dedos válidos", f'{g.get("dedos_validos", 0)}/4'),
        ("Mano", r.get("mano", "—")), ("Referencia", s["referencia_externa"] or "—")])
    return pagina(f"""<div class="c resumen">{insignia}<div class="datos">{resumen}</div></div>
<div class="c"><h2>Captura</h2><div class="fotos">{fotos}</div></div>
<div class="dedos">{dedos}</div>
<div class="c"><p style="margin:0 0 6px">Guardado en <code>ejemplos/datos_proyecto/{sesion_id}/</code> · toca una imagen para ampliarla.</p>
<details><summary>Webhook recibido</summary><pre>{json.dumps(s, ensure_ascii=False, indent=2)}</pre></details>
<a href="/">Volver a empezar</a></div>""")
