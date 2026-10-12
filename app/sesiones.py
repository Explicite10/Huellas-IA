"""Sesiones de captura, solo en memoria: el servicio no escribe huellas ni resultados en disco.

1. El backend del proyecto crea una sesión (POST /api/v1/sesiones) y manda al usuario a url_puente.
2. El usuario captura; el mejor intento vive en memoria mientras dura la sesión.
3. Al aprobarse (o agotar intentos) se entrega todo al backend del proyecto en una sola petición firmada.
4. Si el proyecto confirma (2xx), las huellas se borran de la memoria; solo queda el estado un rato
   (sin datos biométricos) para la página del QR y para consultas.
"""
import asyncio
import io
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import cv2
import numpy as np
import segno
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, field_validator

from . import entrega, pipeline, proyectos
from .config import get_settings

log = logging.getLogger("huellas-api.sesiones")
STATIC = Path(__file__).parent / "static"

api = APIRouter(prefix="/api/v1/sesiones", tags=["Sesiones (backend del proyecto)"])
captura_api = APIRouter(prefix="/api/v1/captura", tags=["Captura (página del usuario)"])
paginas = APIRouter(include_in_schema=False)

# Estados: pendiente -> entregando -> completada | error_entrega ; o expirada / cancelada.
ACTIVOS = ("pendiente", "error_entrega")
TERMINADOS = ("completada", "expirada", "cancelada")
# Envíos automáticos de la página (no gastan intentos si no aprueban): tope por sesión contra abusos.
MAX_INTENTOS_AUTO = 300


def ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(fecha: datetime | None) -> str | None:
    return fecha.isoformat(timespec="seconds") if fecha else None


@dataclass
class Sesion:
    id: str
    token: str
    proyecto_id: str
    referencia_externa: str | None
    mano: str
    calidad_minima: int
    max_intentos: int
    url_retorno: str | None
    webhook_url: str
    url_base: str
    metadata: dict
    creada: datetime
    expira: datetime
    estado: str = "pendiente"
    intentos: int = 0
    intentos_auto: int = 0
    mejor_calidad: float | None = None
    aprobada: bool | None = None
    finalizada: datetime | None = None
    celular_conectado: bool = False
    entrega: dict | None = None
    # Datos biométricos: solo mientras la sesión está activa; se borran al entregar, expirar o cancelar.
    mejor_resultado: dict | None = field(default=None, repr=False)
    mejor_captura: tuple[bytes, str] | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def olvidar_huellas(self) -> None:
        self.mejor_resultado = None
        self.mejor_captura = None


_sesiones: dict[str, Sesion] = {}
_por_token: dict[str, str] = {}


def _guardar(sesion: Sesion) -> None:
    _sesiones[sesion.id] = sesion
    _por_token[sesion.token] = sesion.id


def _quitar(sesion: Sesion) -> None:
    _sesiones.pop(sesion.id, None)
    _por_token.pop(sesion.token, None)


def reiniciar_memoria() -> None:
    _sesiones.clear()
    _por_token.clear()


# ---------------------------------------------------------------- modelos

class CrearSesion(BaseModel):
    referencia_externa: str | None = Field(None, max_length=200,
                                           description="Tu identificador (cliente, trámite…); vuelve en el aviso")
    mano: Literal["auto", "derecha", "izquierda"] = "auto"
    calidad_minima: int | None = Field(None, ge=20, le=95, description="Calidad global para aprobar (0-100)")
    max_intentos: int | None = Field(None, ge=1, le=20)
    url_retorno: str | None = Field(None, description="A dónde regresa el usuario al terminar")
    webhook_url: str | None = Field(None, description="Dónde se entrega el resultado; si no, la del proyecto")
    expira_minutos: int | None = Field(None, ge=5, le=1440)
    metadata: dict = Field(default_factory=dict, description="Datos libres que vuelven en el aviso")

    @field_validator("metadata")
    @classmethod
    def metadata_chica(cls, v: dict) -> dict:
        if len(json.dumps(v)) > 4096:
            raise ValueError("metadata no debe exceder 4 KB")
        return v


# ---------------------------------------------------------------- utilidades

def _con_parametros(url: str, params: dict) -> str:
    p = urlparse(url)
    return urlunparse(p._replace(query=urlencode([*parse_qsl(p.query), *params.items()])))


def _redireccion(s: Sesion) -> str | None:
    if not s.url_retorno:
        return None
    return _con_parametros(s.url_retorno, {"sesion_id": s.id, "estado": s.estado,
                                           "aprobada": "1" if s.aprobada else "0"})


def _origen(url: str | None) -> str | None:
    if not url:
        return None
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def vista(s: Sesion) -> dict:
    activa = s.estado == "pendiente"
    return {
        "id": s.id,
        "referencia_externa": s.referencia_externa,
        "estado": s.estado,
        "aprobada": s.aprobada,
        "mano": s.mano,
        "calidad_minima": s.calidad_minima,
        "max_intentos": s.max_intentos,
        "intentos": s.intentos,
        "mejor_calidad": s.mejor_calidad,
        # Recomendado: en celular abre la cámara directo; en computadora muestra un QR y espera.
        "url_puente": f"{s.url_base}/puente/{s.token}" if activa else None,
        "url_captura": f"{s.url_base}/captura/{s.token}" if activa else None,
        "creada": _iso(s.creada),
        "expira": _iso(s.expira),
        "finalizada": _iso(s.finalizada),
        "entrega": s.entrega,
        "metadata": s.metadata,
    }


def _config_publica(s: Sesion) -> dict:
    proyecto = proyectos.por_id(s.proyecto_id) or {}
    return {
        "sesion_id": s.id,
        "proyecto": proyecto.get("nombre", ""),
        "estado": s.estado,
        "aprobada": s.aprobada,
        "mano": s.mano,
        "calidad_minima": s.calidad_minima,
        "max_intentos": s.max_intentos,
        "intentos": s.intentos,
        "calidad": s.mejor_calidad,
        "expira": _iso(s.expira),
        "redireccion": _redireccion(s) if s.estado in TERMINADOS else None,
        "origen_retorno": _origen(s.url_retorno),
        "celular_conectado": s.celular_conectado,
    }


def _sesion_del_proyecto(sesion_id: str, proyecto: dict) -> Sesion:
    s = _sesiones.get(sesion_id)
    if not s or s.proyecto_id != proyecto["id"]:
        raise HTTPException(status_code=404, detail="Sesión no encontrada (o ya no se recuerda)")
    return s


def _sesion_por_token(token: str) -> Sesion:
    s = _sesiones.get(_por_token.get(token, ""))
    if not s:
        raise HTTPException(status_code=404, detail="Enlace de captura inválido o vencido")
    if s.estado in ACTIVOS and s.expira < ahora():
        _terminar_sin_resultado(s, "expirada")
    return s


def _secreto(s: Sesion) -> str:
    return (proyectos.por_id(s.proyecto_id) or {}).get("webhook_secreto", "")


def _terminar_sin_resultado(s: Sesion, estado: str) -> None:
    """Expirada o cancelada: se descartan las huellas y se avisa al proyecto (sin imágenes, un intento)."""
    s.olvidar_huellas()
    s.estado = estado
    s.finalizada = ahora()
    s.expira = ahora() + timedelta(minutes=get_settings().recordar_estado_minutos)
    asyncio.get_running_loop().create_task(
        entrega.enviar(s.webhook_url, _secreto(s), f"sesion.{estado}", {"sesion": vista(s)}, reintentos=0))


async def finalizar(s: Sesion) -> None:
    """Entrega el mejor intento al proyecto y, si lo confirma, borra las huellas de la memoria."""
    resultado = s.mejor_resultado
    s.aprobada = bool(resultado["calidad_global"]["puntuacion"] >= s.calidad_minima
                      and resultado["calidad_global"]["dedos_validos"] == 4)
    s.estado = "entregando"  # la página del QR espera; aún no se confirma
    limpio, archivos = entrega.separar_imagenes(resultado)
    contenido, tipo = s.mejor_captura
    nombre = "captura.png" if tipo == "image/png" else "captura.jpg"
    archivos.insert(0, (nombre, contenido, tipo))
    limpio["imagenes"] = {"captura": nombre, **limpio["imagenes"]}

    finalizada = ahora()
    sesion_aviso = {**vista(s), "estado": "completada", "finalizada": _iso(finalizada)}
    estado = await entrega.enviar(s.webhook_url, _secreto(s), "sesion.completada",
                                  {"sesion": sesion_aviso, "resultado": limpio}, archivos)
    s.entrega = {k: estado[k] for k in ("ok", "codigo", "error", "intentos", "id")}
    if estado["ok"]:
        s.olvidar_huellas()
        s.estado = "completada"
        s.finalizada = finalizada
        s.expira = ahora() + timedelta(minutes=get_settings().recordar_estado_minutos)
        log.info("Sesión %s entregada (aprobada=%s, calidad=%s)", s.id, s.aprobada, s.mejor_calidad)
    else:
        # Se conservan en memoria hasta que venza la sesión, para que el usuario pueda reintentar el envío.
        s.estado = "error_entrega"


def mantenimiento() -> dict:
    """Vence sesiones abandonadas y olvida las terminadas. Se ejecuta cada pocos segundos."""
    vencidas = olvidadas = 0
    for s in list(_sesiones.values()):
        if s.expira >= ahora() or s.estado == "entregando":
            continue
        if s.estado in ACTIVOS:
            _terminar_sin_resultado(s, "expirada")
            vencidas += 1
        else:
            _quitar(s)
            olvidadas += 1
    return {"expiradas": vencidas, "olvidadas": olvidadas, "en_memoria": len(_sesiones)}


# ---------------------------------------------------------------- API para el backend del proyecto

@api.post("", status_code=201, summary="Crear una sesión de captura")
async def crear_sesion(datos: CrearSesion, request: Request, proyecto: dict = Depends(proyectos.autenticar)):
    cfg = get_settings()
    webhook_url = datos.webhook_url or proyecto.get("webhook_url")
    if not webhook_url:
        raise HTTPException(status_code=422,
                            detail="Falta webhook_url: es el único canal por el que llega el resultado")
    for campo, url in (("url_retorno", datos.url_retorno), ("webhook_url", webhook_url)):
        if url:
            proyectos.validar_url(url, proyecto, campo)
    s = Sesion(
        id="ses_" + uuid.uuid4().hex,
        token=secrets.token_urlsafe(32),
        proyecto_id=proyecto["id"],
        referencia_externa=datos.referencia_externa,
        mano=datos.mano,
        calidad_minima=datos.calidad_minima or cfg.calidad_minima_defecto,
        max_intentos=datos.max_intentos or cfg.max_intentos_defecto,
        url_retorno=datos.url_retorno,
        webhook_url=webhook_url,
        url_base=cfg.url_publica or str(request.base_url).rstrip("/"),
        metadata=datos.metadata,
        creada=ahora(),
        expira=ahora() + timedelta(minutes=datos.expira_minutos or cfg.sesion_minutos),
    )
    _guardar(s)
    return vista(s)


@api.get("", summary="Listar las sesiones del proyecto que siguen en memoria")
async def listar_sesiones(estado: str | None = None, proyecto: dict = Depends(proyectos.autenticar)):
    return [vista(s) for s in sorted(_sesiones.values(), key=lambda s: s.creada, reverse=True)
            if s.proyecto_id == proyecto["id"] and (not estado or s.estado == estado)]


@api.get("/{sesion_id}", summary="Consultar el estado de una sesión (sin huellas)")
async def obtener_sesion(sesion_id: str, proyecto: dict = Depends(proyectos.autenticar)):
    return vista(_sesion_del_proyecto(sesion_id, proyecto))


@api.post("/{sesion_id}/cancelar", summary="Cancelar una sesión activa")
async def cancelar(sesion_id: str, proyecto: dict = Depends(proyectos.autenticar)):
    s = _sesion_del_proyecto(sesion_id, proyecto)
    if s.estado not in ACTIVOS:
        raise HTTPException(status_code=409, detail=f"La sesión ya está {s.estado}")
    _terminar_sin_resultado(s, "cancelada")
    return vista(s)


def _url_captura_qr(s: Sesion) -> str:
    # modo=qr: al terminar, el celular no redirige; la computadora es la que continúa el trámite.
    return f"{s.url_base}/captura/{s.token}?modo=qr"


def _qr_svg(url: str) -> Response:
    buf = io.BytesIO()
    # SVG completo (con xmlns): el inline no se puede usar en <img src>.
    segno.make(url, error="m").save(buf, kind="svg", scale=8, border=2, dark="#111111", light="#ffffff")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@api.get("/{sesion_id}/qr.svg", summary="QR para abrir la captura en el celular (para UIs propias)")
async def qr_sesion(sesion_id: str, proyecto: dict = Depends(proyectos.autenticar)):
    s = _sesion_del_proyecto(sesion_id, proyecto)
    if s.estado != "pendiente":
        raise HTTPException(status_code=409, detail=f"La sesión ya está {s.estado}")
    return _qr_svg(_url_captura_qr(s))


# ---------------------------------------------------------------- API de la página de captura (por token)

@captura_api.get("/{token}")
async def config_captura(token: str, modo: str | None = None):
    s = _sesion_por_token(token)
    if modo == "qr":
        s.celular_conectado = True  # la computadora (página puente) muestra "celular conectado"
    return _config_publica(s)


@captura_api.get("/{token}/qr.svg")
async def qr_captura(token: str):
    s = _sesion_por_token(token)
    if s.estado != "pendiente":
        raise HTTPException(status_code=409, detail=f"La sesión ya está {s.estado}")
    return _qr_svg(_url_captura_qr(s))


@captura_api.post("/{token}/intentos")
async def nuevo_intento(token: str, imagen: UploadFile = File(...), landmarks: str | None = Form(None),
                       automatico: bool = Form(False)):
    """Un intento de captura. Con automatico=true (la página envía sola mientras el usuario acomoda la mano)
    solo cuenta contra max_intentos si aprueba; los que no aprueban igual pueden quedar como la mejor captura."""
    s = _sesion_por_token(token)
    async with s.lock:
        if s.estado != "pendiente":
            raise HTTPException(status_code=409, detail=f"La sesión ya está {s.estado}")
        if s.intentos >= s.max_intentos:
            raise HTTPException(status_code=409, detail="Se alcanzó el máximo de intentos")
        if automatico and s.intentos_auto >= MAX_INTENTOS_AUTO:
            raise HTTPException(status_code=429, detail="Demasiados intentos automáticos; usa la mejor captura")

        datos = await imagen.read()
        img = cv2.imdecode(np.frombuffer(datos, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(status_code=400, detail="No se pudo leer la imagen")
        puntos = None
        if landmarks:
            try:
                puntos = json.loads(landmarks)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="landmarks no es JSON válido")
        try:
            resultado = await asyncio.to_thread(pipeline.procesar, img, puntos, s.mano, True)
        except pipeline.SinManoError as e:
            raise HTTPException(status_code=422, detail=str(e))

        calidad = resultado["calidad_global"]["puntuacion"]
        if s.mejor_calidad is None or calidad > s.mejor_calidad:
            s.mejor_calidad = calidad
            s.mejor_resultado = resultado
            s.mejor_captura = (datos, imagen.content_type or "image/jpeg")

        aprobado = calidad >= s.calidad_minima and resultado["calidad_global"]["dedos_validos"] == 4
        if automatico:
            s.intentos_auto += 1
        if aprobado or not automatico:
            s.intentos += 1
        if aprobado or s.intentos >= s.max_intentos:
            await finalizar(s)
    return {"intento": s.intentos, "intento_aprobado": aprobado, "sesion": _config_publica(s),
            "resultado": resultado}


@captura_api.post("/{token}/finalizar")
async def finalizar_captura(token: str):
    """Usar la mejor captura hasta ahora, o reintentar el envío si el proyecto no respondió."""
    s = _sesion_por_token(token)
    async with s.lock:
        if s.estado not in ACTIVOS:
            raise HTTPException(status_code=409, detail=f"La sesión ya está {s.estado}")
        if not s.mejor_resultado:
            raise HTTPException(status_code=409, detail="Aún no hay ninguna captura")
        await finalizar(s)
    return _config_publica(s)


# ---------------------------------------------------------------- páginas

def _cabeceras_pagina(token: str) -> dict:
    ancestros = ["'self'"]
    s = _sesiones.get(_por_token.get(token, ""))
    if s:
        for d in (proyectos.por_id(s.proyecto_id) or {}).get("dominios_permitidos", []):
            ancestros += [f"https://{d}", f"https://*.{d}"]
    return {
        # Solo los dominios del proyecto pueden incrustar la captura en un iframe.
        "Content-Security-Policy": f"frame-ancestors {' '.join(ancestros)}",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
    }


@paginas.get("/captura/{token}")
async def pagina_captura(token: str):
    return FileResponse(STATIC / "index.html", headers=_cabeceras_pagina(token))


@paginas.get("/fin/{token}")
async def pagina_fin(token: str):
    """Vista final (éxito, calidad insuficiente, error de envío…). Reemplaza a la captura en el historial."""
    return FileResponse(STATIC / "fin.html", headers=_cabeceras_pagina(token))


@paginas.get("/puente/{token}")
async def pagina_puente(token: str):
    """En computadora muestra el QR y espera a que el celular termine; en celular abre la captura."""
    return FileResponse(STATIC / "puente.html", headers=_cabeceras_pagina(token))
