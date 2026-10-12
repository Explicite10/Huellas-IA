"""Calidad por dedo (0-100) y nivel 1-5 al estilo NFIQ.

Es una métrica propia y explicable; no sustituye a NFIQ 2 oficial de NIST
(ver README para integrarlo cuando se requiera certificación).
"""
import numpy as np

from .enhance import ResultadoRealce

NIVELES = [(80, 1, "excelente"), (65, 2, "muy buena"), (50, 3, "buena"), (35, 4, "regular"), (0, 5, "mala")]


def _escala(v: float, bajo: float, alto: float) -> float:
    return float(np.clip((v - bajo) / (alto - bajo), 0, 1))


def nivel(puntuacion: float) -> tuple[int, str]:
    for minimo, n, etiqueta in NIVELES:
        if puntuacion >= minimo:
            return n, etiqueta
    return 5, "mala"


def calidad_dedo(r: ResultadoRealce, ancho_nativo_px: float, ancho_dedo_mm: float, area_prior: float) -> dict:
    coherencia = float(r.coherencia[r.mascara].mean()) if r.mascara.any() else 0.0
    area_util = float(r.mascara_valida.sum() / max(area_prior, 1))
    ppi = ancho_nativo_px / (ancho_dedo_mm / 25.4)

    componentes = {
        "coherencia": _escala(coherencia, 0.20, 0.60),
        "nitidez": _escala(r.energia_banda, 0.08, 0.40),
        "contraste": _escala(r.contraste_local, 0.015, 0.08),
        "area": _escala(area_util, 0.10, 0.50),
        "resolucion": _escala(ppi, 180, 400),
    }
    pesos = {"coherencia": 0.35, "nitidez": 0.25, "contraste": 0.10, "area": 0.15, "resolucion": 0.15}
    puntuacion = 100 * sum(componentes[k] * pesos[k] for k in pesos)
    # Sin resolución mínima no hay crestas reales que medir, aunque el ruido parezca coherente.
    if ppi < 150:
        puntuacion = min(puntuacion, 30)
    n, etiqueta = nivel(puntuacion)
    return {
        "puntuacion": round(puntuacion, 1),
        "nivel": n,
        "etiqueta": etiqueta,
        "coherencia": round(coherencia, 3),
        "nitidez": round(r.energia_banda, 3),
        "contraste": round(r.contraste_local, 4),
        "area_util": round(min(area_util, 1.0), 3),
        "periodo_crestas_px": round(r.periodo, 2),
        "ppi_nativo_estimado": round(ppi),
        "componentes": {k: round(v, 3) for k, v in componentes.items()},
    }
