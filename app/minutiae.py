"""Extracción de minucias por número de cruce (crossing number) sobre el esqueleto de crestas."""
import numpy as np
from scipy.spatial import cKDTree

from .enhance import ResultadoRealce

# Vecinos en orden circular: N, NE, E, SE, S, SO, O, NO.
VECINOS = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]


def numero_cruce(esq: np.ndarray) -> np.ndarray:
    p = np.pad(esq.astype(np.int8), 1)
    h, w = esq.shape
    v = [p[1 + dy:1 + dy + h, 1 + dx:1 + dx + w] for dy, dx in VECINOS]
    cn = sum(np.abs(v[i] - v[(i + 1) % 8]) for i in range(8)) // 2
    return np.where(esq, cn, 0)


def recorrer(esq: np.ndarray, y: int, x: int, primer: tuple[int, int], pasos: int) -> tuple[int, int]:
    """Sigue una rama del esqueleto desde (y, x) pasando por `primer`; devuelve el punto final."""
    h, w = esq.shape
    visitados = {(y, x), primer}
    cy, cx = primer
    for _ in range(pasos):
        sig = None
        for dy, dx in VECINOS:
            ny, nx = cy + dy, cx + dx
            if 0 <= ny < h and 0 <= nx < w and esq[ny, nx] and (ny, nx) not in visitados:
                sig = (ny, nx)
                break
        if sig is None:
            break
        visitados.add(sig)
        cy, cx = sig
    return cy, cx


def ramas(esq: np.ndarray, y: int, x: int, pasos: int) -> list[float]:
    """Ángulos (rad, convención ISO: antihorario, eje y hacia arriba) de cada rama que sale del punto."""
    h, w = esq.shape
    angulos = []
    for dy, dx in VECINOS:
        ny, nx = y + dy, x + dx
        if 0 <= ny < h and 0 <= nx < w and esq[ny, nx]:
            ey, ex = recorrer(esq, y, x, (ny, nx), pasos)
            angulos.append(np.arctan2(-(ey - y), ex - x))
    return angulos


def diferencia_angular(a: float, b: float) -> float:
    return abs((a - b + np.pi) % (2 * np.pi) - np.pi)


def angulo_minucia(esq: np.ndarray, y: int, x: int, tipo: str, pasos: int) -> float:
    rs = ramas(esq, y, x, pasos)
    if not rs:
        return 0.0
    if tipo == "terminacion":
        # La dirección apunta desde la cresta hacia donde termina.
        return (rs[0] + np.pi) % (2 * np.pi)
    if len(rs) >= 3:
        # Las dos ramas más cercanas entre sí forman la horquilla; la dirección es la del tronco opuesto.
        pares = [(diferencia_angular(rs[i], rs[j]), i, j) for i in range(len(rs)) for j in range(i + 1, len(rs))]
        _, i, j = min(pares)
        tronco = next(k for k in range(len(rs)) if k not in (i, j))
        return (rs[tronco] + np.pi) % (2 * np.pi)
    return float(np.mean(rs)) % (2 * np.pi)


def extraer_minucias(r: ResultadoRealce, max_minucias: int = 120) -> list[dict]:
    esq = r.esqueleto
    cn = numero_cruce(esq)
    candidatos = [(y, x, "terminacion") for y, x in zip(*np.where(cn == 1))]
    candidatos += [(y, x, "bifurcacion") for y, x in zip(*np.where(cn == 3))]
    candidatos = [c for c in candidatos if r.mascara_valida[c[0], c[1]]]
    if not candidatos:
        return []

    # Falsas minucias: crestas cortadas, espolones y puentes producen pares muy cercanos.
    pts = np.array([[c[0], c[1]] for c in candidatos], dtype=np.float64)
    arbol = cKDTree(pts)
    descartar = set()
    for i, j in arbol.query_pairs(r.periodo * 1.1):
        descartar.update((i, j))
    pasos = max(int(r.periodo * 1.2), 6)

    minucias = []
    for k, (y, x, tipo) in enumerate(candidatos):
        if k in descartar:
            continue
        angulo = angulo_minucia(esq, y, x, tipo, pasos)
        minucias.append({
            "x": int(x),
            "y": int(y),
            "angulo": round(float(np.degrees(angulo)) % 360, 1),
            "tipo": tipo,
            "confianza": round(float(r.coherencia[y, x]), 3),
        })
    minucias.sort(key=lambda m: -m["confianza"])
    return minucias[:max_minucias]
