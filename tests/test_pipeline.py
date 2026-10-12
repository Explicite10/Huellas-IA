import json

import cv2
import numpy as np
from fastapi.testclient import TestClient

from app import geometry, pipeline
from app.main import app

from .sintetico import imagen_sintetica, landmarks_sinteticos


def test_mano_derecha_por_geometria():
    pts = geometry.a_pixeles(landmarks_sinteticos(), 1920, 1080)
    assert geometry.mano_por_geometria(pts) == "derecha"
    assert geometry.evaluar_postura(pts) == []


def test_crestas_nitidas_dan_buena_calidad():
    r = pipeline.procesar(imagen_sintetica(), landmarks_sinteticos(), "auto")
    assert r["mano"] == "derecha"
    assert [d["posicion_nist"] for d in r["dedos"]] == [2, 3, 4, 5]
    for d in r["dedos"]:
        assert d["estado"] == "ok"
        assert d["calidad"]["puntuacion"] >= 60, d["calidad"]
        assert 7 <= d["calidad"]["periodo_crestas_px"] <= 12
    assert r["calidad_global"]["nivel"] <= 3


def test_ruido_da_mala_calidad():
    r = pipeline.procesar(imagen_sintetica(crestas=False), landmarks_sinteticos(), "auto")
    assert r["calidad_global"]["puntuacion"] < 45
    assert r["estado"] == "calidad_insuficiente"


def test_mano_contraria_genera_advertencia():
    r = pipeline.procesar(imagen_sintetica(), landmarks_sinteticos(), "izquierda", incluir_imagenes=False)
    assert r["mano"] == "izquierda"
    assert any("palma" in a for a in r["advertencias"])
    assert "imagenes" not in r["dedos"][0]


def test_endpoint_captura():
    ok, buf = cv2.imencode(".jpg", imagen_sintetica(), [cv2.IMWRITE_JPEG_QUALITY, 95])
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/huellas/captura",
            files={"imagen": ("mano.jpg", buf.tobytes(), "image/jpeg")},
            data={"landmarks": json.dumps(landmarks_sinteticos()), "mano": "auto"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["dedos"]) == 4
        assert body["dedos"][0]["imagenes"]["realce_gabor"].startswith("data:image/png;base64,")
        assert client.get("/").status_code == 200


def test_endpoint_sin_mano():
    ok, buf = cv2.imencode(".jpg", np.zeros((480, 640, 3), np.uint8))
    with TestClient(app) as client:
        resp = client.post("/api/v1/huellas/captura", files={"imagen": ("x.jpg", buf.tobytes(), "image/jpeg")})
        assert resp.status_code == 422
