"""Entrega del resultado al backend del proyecto, en el momento y sin guardar nada.

Un solo POST multipart/form-data a la webhook_url del proyecto:
    evento      evento.json (application/json): evento, sesión y resultado (las imágenes van por nombre)
    imagenes    un archivo por imagen: captura.jpg, mano_anotada.jpg, indice_original.png, …

Cabeceras:
    X-Huellas-Evento:  sesion.completada | sesion.expirada | sesion.cancelada
    X-Huellas-Entrega: id único del evento (para ignorar duplicados)
    X-Huellas-Firma:   t=<unix>,v1=<hex HMAC-SHA256(webhook_secreto, "<t>." + cuerpo_crudo)>

Solo una respuesta 2xx confirma que el proyecto guardó todo; si no, se reintenta un par de veces al
momento y, si sigue fallando, el usuario ve el error y puede reintentar el envío.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx

from .config import get_settings

log = logging.getLogger("huellas-api.entrega")
TOLERANCIA_SEGUNDOS = 300
# Las pruebas sustituyen el transporte HTTP para simular el backend del proyecto.
transporte: httpx.AsyncBaseTransport | None = None


def firmar(secreto: str, timestamp: int, cuerpo: bytes) -> str:
    return hmac.new(secreto.encode(), f"{timestamp}.".encode() + cuerpo, hashlib.sha256).hexdigest()


def cabecera_firma(secreto: str, cuerpo: bytes, timestamp: int | None = None) -> str:
    t = int(timestamp or time.time())
    return f"t={t},v1={firmar(secreto, t, cuerpo)}"


def verificar(secreto: str, cabecera: str, cuerpo: bytes, tolerancia: int = TOLERANCIA_SEGUNDOS) -> bool:
    """Lo que debe hacer el backend receptor (ver README). Rechaza firmas viejas para evitar reenvíos."""
    try:
        partes = dict(p.split("=", 1) for p in cabecera.split(","))
        t = int(partes["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - t) > tolerancia:
        return False
    return hmac.compare_digest(firmar(secreto, t, cuerpo), partes.get("v1", ""))


def separar_imagenes(resultado: dict) -> tuple[dict, list[tuple[str, bytes, str]]]:
    """Convierte el resultado del pipeline (imágenes en base64) en JSON con nombres + lista de archivos."""
    limpio = json.loads(json.dumps(resultado))
    archivos: list[tuple[str, bytes, str]] = []

    def archivo(nombre: str, data_uri: str) -> str:
        tipo = data_uri[5:data_uri.index(";")]
        archivos.append((nombre, base64.b64decode(data_uri.split(",", 1)[1]), tipo))
        return nombre

    imagenes = {}
    if anotada := limpio.pop("imagen_anotada", None):
        imagenes["mano_anotada"] = archivo("mano_anotada.jpg", anotada)
    for dedo in limpio["dedos"]:
        if "imagenes" in dedo:
            dedo["imagenes"] = {etapa: archivo(f"{dedo['dedo']}_{etapa}.png", uri)
                                for etapa, uri in dedo["imagenes"].items()}
    limpio["imagenes"] = imagenes
    return limpio, archivos


def armar_peticion(cliente: httpx.AsyncClient, url: str, secreto: str, evento: str, datos: dict,
                   archivos: list[tuple[str, bytes, str]]) -> httpx.Request:
    cuerpo_evento = {
        "id": "evt_" + uuid.uuid4().hex,
        "evento": evento,
        "creado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "datos": datos,
    }
    partes = [("evento", ("evento.json", json.dumps(cuerpo_evento, ensure_ascii=False).encode(), "application/json"))]
    partes += [("imagenes", (nombre, contenido, tipo)) for nombre, contenido, tipo in archivos]
    peticion = cliente.build_request("POST", url, files=partes, headers={
        "User-Agent": "huellas-api/1.0",
        "X-Huellas-Evento": evento,
        "X-Huellas-Entrega": cuerpo_evento["id"],
    })
    # La firma cubre el cuerpo crudo completo (JSON + imágenes).
    peticion.headers["X-Huellas-Firma"] = cabecera_firma(secreto, peticion.read())
    return peticion


async def enviar(url: str, secreto: str, evento: str, datos: dict,
                 archivos: list[tuple[str, bytes, str]] | None = None, reintentos: int | None = None) -> dict:
    """Envía y espera la confirmación. Devuelve {ok, codigo, error, intentos, id}."""
    s = get_settings()
    reintentos = s.entrega_reintentos if reintentos is None else reintentos
    estado = {"ok": False, "codigo": None, "error": None, "intentos": 0, "id": None}
    async with httpx.AsyncClient(transport=transporte, timeout=s.entrega_timeout) as cliente:
        peticion = armar_peticion(cliente, url, secreto, evento, datos, archivos or [])
        estado["id"] = peticion.headers["X-Huellas-Entrega"]
        for intento in range(reintentos + 1):
            if intento:
                await asyncio.sleep(intento)  # 1 s, 2 s…
            estado["intentos"] = intento + 1
            try:
                r = await cliente.send(peticion)
                estado["codigo"] = r.status_code
                if 200 <= r.status_code < 300:
                    estado.update(ok=True, error=None)
                    log.info("Evento %s entregado a %s", evento, url)
                    return estado
                estado["error"] = f"HTTP {r.status_code}: {r.text[:200]}"
            except httpx.HTTPError as e:
                estado["error"] = f"{type(e).__name__}: {e}"
            log.warning("Entrega de %s a %s falló (intento %d): %s", evento, url, intento + 1, estado["error"])
    return estado
