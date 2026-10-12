"""Detección de la mano en el servidor (para imágenes subidas sin landmarks del navegador)."""
import threading

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

from .config import get_settings

_lock = threading.Lock()
_detector: vision.HandLandmarker | None = None


def _obtener() -> vision.HandLandmarker:
    global _detector
    if _detector is None:
        opciones = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(get_settings().modelo_mano)),
            running_mode=vision.RunningMode.IMAGE,
            num_hands=1,
            min_hand_detection_confidence=0.3,
            min_hand_presence_confidence=0.3,
        )
        _detector = vision.HandLandmarker.create_from_options(opciones)
    return _detector


def warmup() -> None:
    detectar(np.zeros((256, 256, 3), np.uint8))


def detectar(bgr: np.ndarray) -> list[dict] | None:
    """Landmarks normalizados (x, y en [0,1]) de la mano más visible, o None."""
    h, w = bgr.shape[:2]
    # El detector trabaja internamente a baja resolución; reducir acelera sin perder precisión.
    escala = min(1.0, 1280 / max(h, w))
    img = cv2.resize(bgr, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA) if escala < 1 else bgr
    rgb = np.ascontiguousarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    with _lock:
        res = _obtener().detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    if not res.hand_landmarks:
        return None
    return [{"x": p.x, "y": p.y, "z": p.z} for p in res.hand_landmarks[0]]
