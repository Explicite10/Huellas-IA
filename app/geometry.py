"""Geometría de la mano a partir de los 21 landmarks de MediaPipe.

La misma lógica está replicada en static/app.js para la evaluación en tiempo real.
"""
from dataclasses import dataclass

import cv2
import numpy as np

# (nombre, MCP, PIP, DIP, TIP) en la numeración de MediaPipe Hands.
DEDOS = [
    ("indice", 5, 6, 7, 8),
    ("medio", 9, 10, 11, 12),
    ("anular", 13, 14, 15, 16),
    ("menique", 17, 18, 19, 20),
]
# Códigos de posición ANSI/NIST-ITL (1-5 mano derecha, 6-10 izquierda).
POSICION_NIST = {
    "derecha": {"indice": 2, "medio": 3, "anular": 4, "menique": 5},
    "izquierda": {"indice": 7, "medio": 8, "anular": 9, "menique": 10},
}
SLAP_NIST = {"derecha": 13, "izquierda": 14}


@dataclass
class RegionDedo:
    nombre: str
    inicio: np.ndarray   # punto de la base de la región (cerca del pliegue DIP), px
    fin: np.ndarray      # punto superior (más allá de la punta), px
    ancho: float         # ancho estimado del dedo, px nativos
    eje: np.ndarray      # vector unitario base -> punta

    @property
    def largo(self) -> float:
        return float(np.linalg.norm(self.fin - self.inicio))

    @property
    def centro(self) -> np.ndarray:
        return (self.inicio + self.fin) / 2

    @property
    def angulo_grados(self) -> float:
        """Ángulo de la punta respecto a la vertical (0 = dedo apuntando hacia arriba)."""
        return float(np.degrees(np.arctan2(self.eje[0], -self.eje[1])))

    def esquinas(self, margen: float = 1.0) -> np.ndarray:
        r = np.array([-self.eje[1], self.eje[0]])
        m = self.ancho * margen / 2
        return np.array([self.fin - m * r, self.fin + m * r, self.inicio + m * r, self.inicio - m * r])


def a_pixeles(landmarks: list[dict], ancho: int, alto: int) -> np.ndarray:
    return np.array([[p["x"] * ancho, p["y"] * alto] for p in landmarks], dtype=np.float64)


def regiones_dedos(pts: np.ndarray) -> list[RegionDedo]:
    mcps = [pts[d[1]] for d in DEDOS]
    regiones = []
    for i, (nombre, mcp, pip, dip, tip) in enumerate(DEDOS):
        p_dip, p_tip = pts[dip], pts[tip]
        seg = float(np.linalg.norm(p_tip - p_dip)) or 1.0
        eje = (p_tip - p_dip) / seg
        vecinos = [np.linalg.norm(mcps[i] - mcps[j]) for j in (i - 1, i + 1) if 0 <= j < 4]
        ancho = float(np.clip(np.mean(vecinos) * 0.92, 0.75 * seg, 1.6 * seg))
        regiones.append(RegionDedo(
            nombre=nombre,
            inicio=p_dip - 0.15 * seg * eje,
            fin=p_tip + 0.45 * seg * eje,
            ancho=ancho,
            eje=eje,
        ))
    return regiones


def signo_palma(pts: np.ndarray) -> float:
    """>0: mano derecha con la palma hacia la cámara (o izquierda de dorso). <0: lo contrario."""
    u = pts[9] - pts[0]
    v = pts[5] - pts[17]
    return float(u[0] * v[1] - u[1] * v[0])


def mano_por_geometria(pts: np.ndarray) -> str:
    return "derecha" if signo_palma(pts) > 0 else "izquierda"


def evaluar_postura(pts: np.ndarray) -> list[str]:
    """Advertencias de postura: dedos doblados o pegados."""
    avisos = []
    for nombre, mcp, pip, dip, tip in DEDOS:
        a = pts[pip] - pts[mcp]
        b = pts[tip] - pts[dip]
        cos = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
        if cos < 0.75:
            avisos.append(f"El dedo {nombre} parece doblado")
    regiones = regiones_dedos(pts)
    for i in range(3):
        r1, r2 = regiones[i], regiones[i + 1]
        dist = np.linalg.norm(pts[DEDOS[i][4]] - pts[DEDOS[i + 1][4]])
        if dist < 0.6 * (r1.ancho + r2.ancho) / 2:
            avisos.append(f"Los dedos {r1.nombre} y {r2.nombre} están muy juntos")
    return avisos


def recortar_dedo(imagen: np.ndarray, region: RegionDedo, ancho_objetivo: int,
                  margen: float = 1.3) -> tuple[np.ndarray, float, np.ndarray]:
    """Recorte rotado de la falange distal con la punta hacia arriba, escalado a ~500 ppi.

    Devuelve (recorte, escala, matriz_afin) donde la matriz lleva de la imagen original al recorte.
    """
    escala = ancho_objetivo / region.ancho
    w = int(round(region.ancho * margen * escala))
    h = int(round(region.largo * escala))
    tl, tr, _, bl = region.esquinas(margen)
    src = np.float32([tl, tr, bl])
    dst = np.float32([[0, 0], [w, 0], [0, h]])
    m = cv2.getAffineTransform(src, dst)
    interp = cv2.INTER_CUBIC if escala > 1 else cv2.INTER_AREA
    recorte = cv2.warpAffine(imagen, m, (w, h), flags=interp, borderMode=cv2.BORDER_REPLICATE)
    return recorte, escala, m
