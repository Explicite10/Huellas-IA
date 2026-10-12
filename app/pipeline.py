import base64
import time
import uuid
from datetime import datetime, timezone

import cv2
import numpy as np

from . import geometry, hand_detector
from .config import get_settings
from .enhance import imagen_binaria, imagen_gabor, mascara_prior, realzar
from .minutiae import extraer_minucias
from .quality import calidad_dedo, nivel

COLORES_NIVEL = {1: (60, 200, 60), 2: (60, 200, 140), 3: (40, 200, 230), 4: (40, 140, 240), 5: (60, 60, 230)}


class SinManoError(ValueError):
    pass


def png_b64(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", img)
    return "data:image/png;base64," + base64.b64encode(buf).decode()


def jpg_b64(img: np.ndarray, calidad: int = 85) -> str:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, calidad])
    return "data:image/jpeg;base64," + base64.b64encode(buf).decode()


def dibujar_minucias(base_gris: np.ndarray, esqueleto: np.ndarray, minucias: list[dict]) -> np.ndarray:
    img = cv2.cvtColor(base_gris, cv2.COLOR_GRAY2BGR)
    img[esqueleto] = (40, 40, 220)
    for m in minucias:
        x, y, a = m["x"], m["y"], np.radians(m["angulo"])
        fin = (int(x + 14 * np.cos(a)), int(y - 14 * np.sin(a)))
        if m["tipo"] == "terminacion":
            cv2.circle(img, (x, y), 5, (40, 170, 40), 2)
            cv2.line(img, (x, y), fin, (40, 170, 40), 2)
        else:
            cv2.rectangle(img, (x - 4, y - 4), (x + 4, y + 4), (210, 120, 20), 2)
            cv2.line(img, (x, y), fin, (210, 120, 20), 2)
    return img


def dentro_de_imagen(region: geometry.RegionDedo, w: int, h: int, tolerancia: float = 0.08) -> bool:
    esq = region.esquinas(0.9)
    tx, ty = w * tolerancia * 0.25, h * tolerancia * 0.25
    return bool((esq[:, 0] > -tx).all() and (esq[:, 0] < w + tx).all()
                and (esq[:, 1] > -ty).all() and (esq[:, 1] < h + ty).all())


def procesar_dedo(img: np.ndarray, region: geometry.RegionDedo, mano: str, incluir_imagenes: bool) -> dict:
    s = get_settings()
    recorte, escala, _ = geometry.recortar_dedo(img, region, s.ancho_dedo_objetivo)
    if s.espejar_compatible_contacto:
        recorte = cv2.flip(recorte, 1)
    r = realzar(recorte, s.ancho_dedo_objetivo)
    area_prior = mascara_prior(*recorte.shape[:2], s.ancho_dedo_objetivo).sum()
    calidad = calidad_dedo(r, region.ancho, s.ancho_dedo_mm, area_prior)
    minucias = extraer_minucias(r)

    dedo = {
        "dedo": region.nombre,
        "posicion_nist": geometry.POSICION_NIST[mano][region.nombre],
        "nombre": f"{mano}_{region.nombre}",
        "estado": "ok",
        "calidad": calidad,
        "region": {
            "centro": [round(float(v), 1) for v in region.centro],
            "ancho_px": round(region.ancho, 1),
            "largo_px": round(region.largo, 1),
            "angulo_grados": round(region.angulo_grados, 1),
        },
        "imagen_huella": {
            "ancho": int(recorte.shape[1]),
            "alto": int(recorte.shape[0]),
            "ppi_normalizado": round(s.ancho_dedo_objetivo / (s.ancho_dedo_mm / 25.4)),
            "escala_aplicada": round(escala, 3),
            "espejada_compatible_contacto": s.espejar_compatible_contacto,
        },
        "minucias_total": len(minucias),
        "minucias": minucias,
    }
    if incluir_imagenes:
        gabor = imagen_gabor(r)
        dedo["imagenes"] = {
            "original": png_b64(recorte),
            "contraste": png_b64(r.contraste),
            "realce_gabor": png_b64(gabor),
            "binaria": png_b64(imagen_binaria(r)),
            "minucias": png_b64(dibujar_minucias(gabor, r.esqueleto, minucias)),
        }
    return dedo


def anotar_mano(img: np.ndarray, pts: np.ndarray, regiones, dedos: list[dict]) -> np.ndarray:
    vis = img.copy()
    grosor = max(2, int(max(img.shape[:2]) / 400))
    for (a, b) in [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 9), (9, 13), (13, 17), (0, 17)] + \
                  [(d[i], d[i + 1]) for d in geometry.DEDOS for i in range(1, 4)]:
        cv2.line(vis, tuple(pts[a].astype(int)), tuple(pts[b].astype(int)), (255, 255, 255), grosor)
    for region, dedo in zip(regiones, dedos):
        n = dedo.get("calidad", {}).get("nivel", 5)
        color = COLORES_NIVEL[n] if dedo["estado"] == "ok" else (120, 120, 120)
        cv2.polylines(vis, [region.esquinas().astype(np.int32)], True, color, grosor * 2)
    escala = min(1.0, 1280 / max(vis.shape[:2]))
    return cv2.resize(vis, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)


def procesar(img: np.ndarray, landmarks: list[dict] | None, mano_solicitada: str = "auto",
             incluir_imagenes: bool = True) -> dict:
    t0 = time.perf_counter()
    s = get_settings()
    h, w = img.shape[:2]
    if max(h, w) > s.max_lado_imagen:
        f = s.max_lado_imagen / max(h, w)
        img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]

    origen = "navegador"
    if not landmarks or len(landmarks) != 21:
        landmarks = hand_detector.detectar(img)
        origen = "servidor"
        if landmarks is None:
            raise SinManoError("No se detectó ninguna mano en la imagen")

    pts = geometry.a_pixeles(landmarks, w, h)
    advertencias = geometry.evaluar_postura(pts)
    mano_geo = geometry.mano_por_geometria(pts)
    if mano_solicitada in ("derecha", "izquierda"):
        mano = mano_solicitada
        if mano_geo != mano:
            advertencias.append("La orientación no coincide con la mano indicada: muestra las yemas (palma) a la cámara")
    else:
        mano = mano_geo

    regiones = geometry.regiones_dedos(pts)
    dedos = []
    for region in regiones:
        if not dentro_de_imagen(region, w, h):
            dedos.append({
                "dedo": region.nombre,
                "posicion_nist": geometry.POSICION_NIST[mano][region.nombre],
                "nombre": f"{mano}_{region.nombre}",
                "estado": "fuera_de_encuadre",
            })
            advertencias.append(f"El dedo {region.nombre} está fuera del encuadre")
            continue
        dedos.append(procesar_dedo(img, region, mano, incluir_imagenes))

    validos = [d for d in dedos if d["estado"] == "ok"]
    puntuaciones = [d["calidad"]["puntuacion"] for d in validos]
    # La calidad del conjunto la marca el peor dedo: un registro sirve si sirven los 4.
    global_ = (0.5 * np.mean(puntuaciones) + 0.5 * min(puntuaciones)) if puntuaciones else 0.0
    n, etiqueta = nivel(global_)

    resultado = {
        "id": str(uuid.uuid4()),
        "fecha": datetime.now(timezone.utc).isoformat(),
        "estado": "ok" if len(validos) == 4 and n <= 3 else "calidad_insuficiente",
        "mano": mano,
        "posicion_nist_slap": geometry.SLAP_NIST[mano],
        "landmarks_origen": origen,
        "imagen": {"ancho": w, "alto": h},
        "calidad_global": {
            "puntuacion": round(float(global_), 1),
            "nivel": n,
            "etiqueta": etiqueta,
            "dedos_validos": len(validos),
        },
        "advertencias": advertencias,
        "dedos": dedos,
    }
    if incluir_imagenes:
        resultado["imagen_anotada"] = jpg_b64(anotar_mano(img, pts, regiones, dedos))
    resultado["tiempo_ms"] = round((time.perf_counter() - t0) * 1000)
    return resultado
