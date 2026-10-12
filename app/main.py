import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import hand_detector, pipeline, sesiones
from .config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("huellas-api")
STATIC = Path(__file__).parent / "static"


async def tareas_de_fondo() -> None:
    """Vence sesiones abandonadas y olvida las terminadas (todo vive en memoria)."""
    while True:
        try:
            sesiones.mantenimiento()
        except Exception:
            log.exception("Error en tareas de fondo")
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Cargando detector de manos...")
    await asyncio.to_thread(hand_detector.warmup)
    tarea = asyncio.create_task(tareas_de_fondo())
    yield
    tarea.cancel()


app = FastAPI(
    title="Huellas sin contacto API",
    version="1.0.0",
    description="Captura de 4 dedos (índice a meñique) con cámara, realce de crestas, "
                "calidad por dedo y extracción de minucias.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(sesiones.api)
app.include_router(sesiones.captura_api)
app.include_router(sesiones.paginas)


def check_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = get_settings().api_key
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="API key inválida")


def solo_demo() -> None:
    if not get_settings().demo:
        raise HTTPException(status_code=404, detail="No encontrado")


@app.get("/", include_in_schema=False, dependencies=[Depends(solo_demo)])
async def ui() -> FileResponse:
    """Página de demostración: cámara con evaluación en tiempo real + resultado gráfico y JSON."""
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/v1/huellas/captura", tags=["Demo"], dependencies=[Depends(solo_demo), Depends(check_api_key)])
async def captura(
    imagen: UploadFile = File(..., description="Foto de los 4 dedos (JPG/PNG)"),
    landmarks: str | None = Form(None, description="JSON con los 21 landmarks normalizados de MediaPipe (opcional)"),
    mano: str = Form("auto", pattern="^(auto|derecha|izquierda)$"),
    incluir_imagenes: bool = Form(True),
):
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
        return await asyncio.to_thread(pipeline.procesar, img, puntos, mano, incluir_imagenes)
    except pipeline.SinManoError as e:
        raise HTTPException(status_code=422, detail=str(e))
