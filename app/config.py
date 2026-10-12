import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    modelo_mano: Path
    # Ancho (px) al que se normaliza la falange distal: ~16 mm a ~500 ppi.
    ancho_dedo_objetivo: int
    # Ancho promedio de la falange distal en mm, para estimar los ppi nativos.
    ancho_dedo_mm: float
    max_lado_imagen: int
    # Las fotos sin contacto son el espejo de una huella entintada/de escáner.
    espejar_compatible_contacto: bool

    # --- Servicio central de captura (sin almacenamiento) ---
    # Proyectos cliente (API key en hash, secreto de firma, dominios). Es configuración, no datos de usuarios.
    archivo_proyectos: Path
    # URL con la que los usuarios abren la captura. Si no se define se usa la URL de la petición.
    url_publica: str | None
    sesion_minutos: int        # tiempo que tiene el usuario para completar la captura
    # Tras terminar, cuánto se recuerda el ESTADO de la sesión (sin huellas) para la página del QR y consultas.
    recordar_estado_minutos: int
    max_intentos_defecto: int
    calidad_minima_defecto: int
    entrega_timeout: float     # segundos que se espera al backend del proyecto en cada intento
    entrega_reintentos: int    # reintentos inmediatos si el backend del proyecto falla
    # Permite webhook_url / url_retorno con http:// (solo desarrollo local).
    permitir_http: bool
    # Muestra la página de demostración en "/" y el endpoint directo /api/v1/huellas/captura.
    demo: bool


@lru_cache
def get_settings() -> Settings:
    return Settings(
        api_key=os.getenv("HUELLAS_API_KEY") or None,
        modelo_mano=Path(os.getenv("HUELLAS_MODELO_MANO", BASE_DIR / "models" / "hand_landmarker.task")),
        ancho_dedo_objetivo=int(os.getenv("HUELLAS_ANCHO_DEDO_PX", "320")),
        ancho_dedo_mm=float(os.getenv("HUELLAS_ANCHO_DEDO_MM", "16")),
        max_lado_imagen=int(os.getenv("HUELLAS_MAX_LADO", "4096")),
        espejar_compatible_contacto=os.getenv("HUELLAS_ESPEJAR", "1") == "1",
        archivo_proyectos=Path(os.getenv("HUELLAS_PROYECTOS", BASE_DIR.parent / "config" / "proyectos.json")),
        url_publica=(os.getenv("HUELLAS_URL_PUBLICA") or "").rstrip("/") or None,
        sesion_minutos=int(os.getenv("HUELLAS_SESION_MINUTOS", "30")),
        recordar_estado_minutos=int(os.getenv("HUELLAS_RECORDAR_ESTADO_MINUTOS", "60")),
        max_intentos_defecto=int(os.getenv("HUELLAS_MAX_INTENTOS", "6")),
        calidad_minima_defecto=int(os.getenv("HUELLAS_CALIDAD_MINIMA", "65")),
        entrega_timeout=float(os.getenv("HUELLAS_ENTREGA_TIMEOUT", "20")),
        entrega_reintentos=int(os.getenv("HUELLAS_ENTREGA_REINTENTOS", "2")),
        permitir_http=os.getenv("HUELLAS_PERMITIR_HTTP", "0") == "1",
        demo=os.getenv("HUELLAS_DEMO", "1") == "1",
    )
