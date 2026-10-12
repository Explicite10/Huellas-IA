"""Realce de crestas papilares para fotos de dedos sin contacto.

Pipeline clásico (Hong, Wan & Jain 1998) adaptado a fotografía:
segmentación del dedo -> aplanado de iluminación + CLAHE -> campo de orientación ->
periodo de crestas por FFT -> banco de filtros de Gabor orientados -> binarización -> esqueleto.
"""
from dataclasses import dataclass

import cv2
import numpy as np
from skimage.morphology import skeletonize

N_ORIENTACIONES = 16
PERIODO_MIN, PERIODO_MAX = 4.5, 20.0


@dataclass
class ResultadoRealce:
    gris: np.ndarray            # uint8, gris original del recorte
    contraste: np.ndarray       # uint8, iluminación aplanada + CLAHE
    gabor: np.ndarray           # float32, respuesta del banco de Gabor (positivo = cresta)
    binaria: np.ndarray         # bool, crestas
    esqueleto: np.ndarray       # bool
    mascara: np.ndarray         # bool, área de huella útil
    mascara_valida: np.ndarray  # bool, área confiable para minucias (lejos del borde y coherente)
    orientacion: np.ndarray     # float32, dirección normal a las crestas, rad en [0, pi)
    coherencia: np.ndarray      # float32 en [0, 1]
    periodo: float              # px entre crestas
    energia_banda: float        # fracción de energía espectral en la banda de crestas
    contraste_local: float      # desviación estándar normalizada dentro de la máscara


def gris_piel(bgr: np.ndarray) -> np.ndarray:
    """En la piel el canal verde es el que mejor contrasta crestas y valles."""
    b, g, r = cv2.split(bgr.astype(np.float32))
    return 0.15 * b + 0.6 * g + 0.25 * r


def mascara_prior(h: int, w: int, ancho_dedo: float) -> np.ndarray:
    """Silueta esperada de la yema: franja centrada con punta redondeada."""
    m = np.zeros((h, w), np.uint8)
    cx, mitad = w // 2, int(ancho_dedo * 0.47)
    radio = int(mitad * 1.15)
    top = int(h * 0.04)
    cv2.rectangle(m, (cx - mitad, top + radio), (cx + mitad, h - 1), 255, -1)
    cv2.ellipse(m, (cx, top + radio), (mitad, radio), 0, 180, 360, 255, -1)
    return m > 0


def segmentar(bgr: np.ndarray, ancho_dedo: float) -> np.ndarray:
    h, w = bgr.shape[:2]
    prior = mascara_prior(h, w, ancho_dedo)
    area_prior = prior.sum()

    ycrcb = cv2.cvtColor(cv2.GaussianBlur(bgr, (0, 0), 3), cv2.COLOR_BGR2YCrCb)
    piel = cv2.inRange(ycrcb, (20, 128, 65), (255, 185, 140)) > 0
    candidata = prior & piel
    if candidata.sum() < 0.4 * area_prior:
        # Iluminación rara (linterna, luz fría): el dedo suele ser lo más claro del recorte.
        g = cv2.GaussianBlur(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (0, 0), 5)
        _, otsu = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        candidata = prior & (otsu > 0)
    if candidata.sum() < 0.4 * area_prior:
        candidata = prior

    m = candidata.astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
    n, etiquetas, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=4)
    if n > 1:
        mayor = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        m = (etiquetas == mayor).astype(np.uint8)
    # Rellenar huecos (brillos especulares dentro de la yema).
    contornos, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    relleno = np.zeros_like(m)
    cv2.drawContours(relleno, contornos, -1, 1, -1)
    return relleno > 0


def aplanar(gris: np.ndarray, mascara: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """Quita el sombreado de la curvatura del dedo y normaliza a media 0 / desviación 1.

    También devuelve la modulación relativa de intensidad (contraste real de las crestas).
    """
    fondo = cv2.GaussianBlur(gris, (0, 0), 10)
    plano = gris / (fondo + 1.0)
    vals = plano[mascara]
    media, std = float(vals.mean()), float(vals.std() + 1e-6)
    norm = np.clip((plano - media) / std, -3, 3).astype(np.float32)
    norm[~mascara] = 0
    vis = ((norm + 3) * (255 / 6)).astype(np.uint8)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    contraste = clahe.apply(vis)
    contraste[~mascara] = 255
    # La señal para Gabor se toma del CLAHE (mejor contraste local).
    señal = (contraste.astype(np.float32) - 127.5) / 64.0
    señal[~mascara] = 0
    return contraste, señal, std / (media + 1e-6)


def campo_orientacion(img: np.ndarray, periodo: float) -> tuple[np.ndarray, np.ndarray]:
    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(img, cv2.CV_32F, 0, 1, ksize=3)
    s = max(periodo * 1.2, 4.0)
    gxx = cv2.GaussianBlur(gx * gx, (0, 0), s)
    gyy = cv2.GaussianBlur(gy * gy, (0, 0), s)
    gxy = cv2.GaussianBlur(gx * gy, (0, 0), s)
    dif = gxx - gyy
    coherencia = np.sqrt(dif ** 2 + 4 * gxy ** 2) / (gxx + gyy + 1e-6)
    # Suavizado del ángulo doble para un campo continuo.
    c2 = cv2.GaussianBlur(dif, (0, 0), s)
    s2 = cv2.GaussianBlur(2 * gxy, (0, 0), s)
    orientacion = np.mod(0.5 * np.arctan2(s2, c2), np.pi).astype(np.float32)
    return orientacion, np.clip(coherencia, 0, 1).astype(np.float32)


def estimar_periodo(señal: np.ndarray, mascara: np.ndarray) -> tuple[float, float]:
    """Periodo dominante de crestas y fracción de energía en esa banda (indicador de nitidez)."""
    ys, xs = np.where(mascara)
    if len(ys) < 100:
        return 9.0, 0.0
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    reg = señal[y0:y1, x0:x1] * mascara[y0:y1, x0:x1]
    reg = reg * np.outer(np.hanning(reg.shape[0]), np.hanning(reg.shape[1]))
    n = 512
    espectro = np.abs(np.fft.fftshift(np.fft.fft2(reg, s=(n, n)))) ** 2
    yy, xx = np.indices((n, n))
    radio = np.hypot(yy - n // 2, xx - n // 2)
    frec = radio / n  # ciclos por pixel
    perfil = np.bincount(radio.astype(int).ravel(), espectro.ravel())
    conteo = np.bincount(radio.astype(int).ravel())
    perfil = perfil / np.maximum(conteo, 1)
    r_min, r_max = int(n / PERIODO_MAX), int(n / PERIODO_MIN)
    pico = r_min + int(np.argmax(perfil[r_min:r_max + 1]))
    periodo = float(n / pico)
    f0 = 1.0 / periodo
    banda = (frec > f0 * 0.7) & (frec < f0 * 1.4)
    total = (frec > 1 / 40) & (frec < 0.5)
    energia = float(espectro[banda].sum() / (espectro[total].sum() + 1e-9))
    return periodo, energia


def banco_gabor(señal: np.ndarray, orientacion: np.ndarray, periodo: float) -> np.ndarray:
    k = int(periodo * 3) | 1
    sigma = 0.5 * periodo
    respuestas = np.empty((N_ORIENTACIONES,) + señal.shape, np.float32)
    for i in range(N_ORIENTACIONES):
        theta = i * np.pi / N_ORIENTACIONES
        ker = cv2.getGaborKernel((k, k), sigma, theta, periodo, 0.6, 0, ktype=cv2.CV_32F)
        ker -= ker.mean()
        ker /= np.abs(ker).sum()
        respuestas[i] = cv2.filter2D(señal, cv2.CV_32F, ker)
    idx = np.round(orientacion / (np.pi / N_ORIENTACIONES)).astype(int) % N_ORIENTACIONES
    return np.take_along_axis(respuestas, idx[None], axis=0)[0]


def realzar(bgr: np.ndarray, ancho_dedo: float) -> ResultadoRealce:
    mascara = segmentar(bgr, ancho_dedo)
    gris = gris_piel(bgr)
    contraste, señal, modulacion = aplanar(gris, mascara)

    periodo, energia = estimar_periodo(señal, mascara)
    orientacion, coherencia = campo_orientacion(señal, periodo)
    gabor = banco_gabor(señal, orientacion, periodo)
    gabor[~mascara] = 0

    # Región confiable: lejos del borde del dedo y con flujo de crestas claro.
    borde = int(periodo * 1.5) | 1
    interior = cv2.erode(mascara.astype(np.uint8), np.ones((borde, borde), np.uint8)) > 0
    # Las crestas se cortan en el borde de `interior`: las minucias ahí son artificiales.
    lejos = int(periodo * 3) | 1
    nucleo = cv2.erode(mascara.astype(np.uint8), np.ones((lejos, lejos), np.uint8)) > 0
    mascara_valida = nucleo & (cv2.GaussianBlur(coherencia, (0, 0), periodo) > 0.25)

    # En fotos sin contacto con luz frontal las crestas reflejan más que los valles.
    binaria = (gabor > 0) & interior
    n, etiquetas, stats, _ = cv2.connectedComponentsWithStats(binaria.astype(np.uint8), connectivity=8)
    pequeñas = np.where(stats[:, cv2.CC_STAT_AREA] < periodo * 3)[0]
    binaria[np.isin(etiquetas, pequeñas[pequeñas > 0])] = False
    esqueleto = skeletonize(binaria)

    return ResultadoRealce(
        gris=np.clip(gris, 0, 255).astype(np.uint8), contraste=contraste, gabor=gabor,
        binaria=binaria, esqueleto=esqueleto, mascara=mascara, mascara_valida=mascara_valida,
        orientacion=orientacion, coherencia=coherencia, periodo=periodo,
        energia_banda=energia, contraste_local=modulacion,
    )


def imagen_gabor(r: ResultadoRealce) -> np.ndarray:
    """Crestas negras sobre fondo blanco, como una huella entintada."""
    vals = r.gabor[r.mascara]
    escala = 3 * (vals.std() + 1e-6) if vals.size else 1.0
    img = 255 - np.clip(127.5 + r.gabor / escala * 127.5, 0, 255)
    img[~r.mascara] = 255
    return img.astype(np.uint8)


def imagen_binaria(r: ResultadoRealce) -> np.ndarray:
    return np.where(r.binaria, 0, 255).astype(np.uint8)
