"""Proyectos cliente del servicio, leídos de un archivo de configuración (config/proyectos.json).

Cada proyecto tiene su API key (se guarda solo su hash), el secreto con el que se firman sus avisos y los
dominios a los que puede regresar al usuario / recibir avisos. El archivo se recarga solo al modificarse.
"""
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import Header, HTTPException

from .config import get_settings

_lock = threading.Lock()
_cache: dict = {"ruta": None, "mtime": None, "proyectos": []}


def _hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def cargar() -> list[dict]:
    ruta = get_settings().archivo_proyectos
    with _lock:
        mtime = ruta.stat().st_mtime if ruta.exists() else None
        if _cache["ruta"] != ruta or _cache["mtime"] != mtime:
            _cache.update(ruta=ruta, mtime=mtime,
                          proyectos=json.loads(ruta.read_text()) if mtime is not None else [])
        return _cache["proyectos"]


def _guardar(proyectos: list[dict]) -> None:
    ruta = get_settings().archivo_proyectos
    ruta.parent.mkdir(parents=True, exist_ok=True)
    # Escritura atómica y solo legible por el dueño: contiene los secretos de firma.
    fd, tmp = tempfile.mkstemp(dir=ruta.parent, prefix=".proyectos")
    with os.fdopen(fd, "w") as f:
        json.dump(proyectos, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, ruta)


def crear(nombre: str, webhook_url: str | None = None, dominios: list[str] | None = None) -> dict:
    """Registra un proyecto. La API key solo se devuelve aquí; en el archivo queda su hash."""
    api_key = "hk_" + secrets.token_urlsafe(32)
    proyecto = {
        "id": "prj_" + uuid.uuid4().hex[:16],
        "nombre": nombre,
        "api_key_hash": _hash(api_key),
        "webhook_secreto": "whsec_" + secrets.token_urlsafe(32),
        "webhook_url": webhook_url,
        "dominios_permitidos": [d.lower().strip() for d in (dominios or []) if d.strip()],
        "activo": True,
        "creado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if webhook_url:
        validar_url(webhook_url, proyecto, "webhook_url")
    _guardar([*cargar(), proyecto])
    return {"id": proyecto["id"], "nombre": nombre, "api_key": api_key,
            "webhook_secreto": proyecto["webhook_secreto"], "webhook_url": webhook_url,
            "dominios_permitidos": proyecto["dominios_permitidos"]}


def _modificar(proyecto_id: str, cambios: dict) -> None:
    proyectos = [dict(p) for p in cargar()]
    for p in proyectos:
        if p["id"] == proyecto_id:
            p.update(cambios)
            _guardar(proyectos)
            return
    raise KeyError(f"No existe el proyecto {proyecto_id}")


def rotar_api_key(proyecto_id: str) -> str:
    api_key = "hk_" + secrets.token_urlsafe(32)
    _modificar(proyecto_id, {"api_key_hash": _hash(api_key)})
    return api_key


def desactivar(proyecto_id: str) -> None:
    _modificar(proyecto_id, {"activo": False})


def listar() -> list[dict]:
    return [{k: v for k, v in p.items() if k not in ("api_key_hash", "webhook_secreto")} for p in cargar()]


def por_id(proyecto_id: str) -> dict | None:
    return next((p for p in cargar() if p["id"] == proyecto_id), None)


def autenticar(x_api_key: str | None = Header(default=None, description="API key del proyecto")) -> dict:
    """Dependencia de FastAPI: identifica al proyecto por su API key."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Falta el header X-API-Key")
    h = _hash(x_api_key)
    for p in cargar():
        if p.get("activo", True) and hmac.compare_digest(p["api_key_hash"], h):
            return p
    raise HTTPException(status_code=401, detail="API key inválida")


def validar_url(url: str, proyecto: dict, campo: str) -> None:
    """Evita redirecciones abiertas y avisos hacia dominios ajenos al proyecto."""
    p = urlparse(url)
    if p.scheme != "https" and not (p.scheme == "http" and get_settings().permitir_http):
        raise HTTPException(status_code=422, detail=f"{campo} debe usar https://")
    if not p.hostname:
        raise HTTPException(status_code=422, detail=f"{campo} no es una URL válida")
    dominios = proyecto["dominios_permitidos"]
    if dominios and not any(p.hostname == d or p.hostname.endswith("." + d) for d in dominios):
        raise HTTPException(status_code=422, detail=f"{campo} no pertenece a los dominios permitidos del proyecto")
