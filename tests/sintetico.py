"""Genera una mano sintética (mano derecha, palma a la cámara) con crestas en las yemas."""
import cv2
import numpy as np

W, H = 1920, 1080
# x de cada dedo: índice a la derecha y meñique a la izquierda (mano derecha vista de frente).
X_DEDOS = [1300, 1100, 900, 700]


def landmarks_sinteticos() -> list[dict]:
    pts = [(1000, 1060)]  # muñeca
    pts += [(1450, 950), (1550, 850), (1620, 760), (1680, 680)]  # pulgar
    for x in X_DEDOS:
        pts += [(x, 800), (x, 600), (x, 450), (x, 330)]
    return [{"x": x / W, "y": y / H, "z": 0.0} for x, y in pts]


def imagen_sintetica(periodo: float = 5.5, ruido: float = 6.0, crestas: bool = True, semilla: int = 0) -> np.ndarray:
    rng = np.random.default_rng(semilla)
    img = np.full((H, W, 3), (40, 50, 60), np.uint8)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    piel = np.array([120, 150, 200], np.float32)  # BGR
    for x in X_DEDOS:
        mascara = np.zeros((H, W), np.uint8)
        cv2.rectangle(mascara, (x - 88, 380), (x + 88, H), 255, -1)
        cv2.ellipse(mascara, (x, 380), (88, 95), 0, 180, 360, 255, -1)
        m = mascara > 0
        if crestas:
            # Patrón tipo verticilo con un desplazamiento para generar minucias.
            d = np.hypot((xx - x) * 1.0, (yy - 420) * 0.8)
            fase = 2 * np.pi * d / periodo + 0.8 * np.sin(np.arctan2(yy - 420, xx - x) * 3)
            patron = 0.5 + 0.5 * np.cos(fase)
        else:
            patron = rng.random((H, W)).astype(np.float32)
        sombreado = 0.75 + 0.25 * np.cos((xx - x) / 88 * np.pi / 2)
        for c in range(3):
            canal = piel[c] * sombreado * (0.75 + 0.25 * patron)
            img[..., c][m] = np.clip(canal[m], 0, 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (0, 0), 0.8)
    img = np.clip(img + rng.normal(0, ruido, img.shape), 0, 255).astype(np.uint8)
    return img
