import asyncio
import json
import os
import time
from datetime import timedelta
from email.parser import BytesParser
from email.policy import default as politica_email

import cv2
import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, entrega, main, proyectos, sesiones

from .sintetico import imagen_sintetica, landmarks_sinteticos


class Receptor:
    """Simula el backend del proyecto que recibe la entrega."""

    def __init__(self):
        self.recibidas: list[httpx.Request] = []
        self.codigo = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.recibidas.append(request)
        return httpx.Response(self.codigo, text="ok")


def partes(req: httpx.Request) -> tuple[dict, dict[str, bytes]]:
    """Separa el multipart en (evento JSON, {nombre_archivo: bytes})."""
    msg = BytesParser(policy=politica_email).parsebytes(
        b"Content-Type: " + req.headers["content-type"].encode() + b"\r\n\r\n" + req.content)
    evento, archivos = None, {}
    for parte in msg.iter_parts():
        if parte.get_param("name", header="content-disposition") == "evento":
            evento = json.loads(parte.get_payload(decode=True))
        else:
            archivos[parte.get_filename()] = parte.get_payload(decode=True)
    return evento, archivos


@pytest.fixture
def receptor(monkeypatch):
    r = Receptor()
    monkeypatch.setattr(entrega, "transporte", httpx.MockTransport(r))
    return r


@pytest.fixture
def cliente(tmp_path, monkeypatch, receptor):
    monkeypatch.setenv("HUELLAS_PROYECTOS", str(tmp_path / "config" / "proyectos.json"))
    monkeypatch.setenv("HUELLAS_PERMITIR_HTTP", "1")
    monkeypatch.setenv("HUELLAS_ENTREGA_REINTENTOS", "0")
    config.get_settings.cache_clear()
    sesiones.reiniciar_memoria()

    async def sin_tareas():
        return None

    monkeypatch.setattr(main, "tareas_de_fondo", sin_tareas)
    with TestClient(main.app) as c:
        yield c
    sesiones.reiniciar_memoria()
    config.get_settings.cache_clear()


@pytest.fixture
def proyecto(cliente):
    return proyectos.crear("Créditos", "http://receptor.test/webhooks/huellas", ["receptor.test"])


def crear_sesion(cliente, proyecto, **extra) -> dict:
    cuerpo = {"referencia_externa": "cliente-123", "url_retorno": "http://receptor.test/registro/paso-4",
              "metadata": {"tramite": "alta"}, **extra}
    r = cliente.post("/api/v1/sesiones", json=cuerpo, headers={"X-API-Key": proyecto["api_key"]})
    assert r.status_code == 201, r.text
    return r.json()


def token_de(sesion: dict) -> str:
    return sesion["url_captura"].rsplit("/", 1)[1]


def enviar_intento(cliente, sesion, crestas=True, automatico=False):
    ok, buf = cv2.imencode(".jpg", imagen_sintetica(crestas=crestas), [cv2.IMWRITE_JPEG_QUALITY, 95])
    return cliente.post(f"/api/v1/captura/{token_de(sesion)}/intentos",
                        files={"imagen": ("c.jpg", buf.tobytes(), "image/jpeg")},
                        data={"landmarks": json.dumps(landmarks_sinteticos()), "automatico": str(automatico).lower()})


def test_flujo_completo_entrega_todo_y_no_guarda_nada(cliente, proyecto, receptor, tmp_path):
    sesion = crear_sesion(cliente, proyecto, mano="derecha")
    assert sesion["estado"] == "pendiente" and sesion["url_puente"] and sesion["url_captura"]

    r = enviar_intento(cliente, sesion)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["intento_aprobado"] and cuerpo["sesion"]["estado"] == "completada"
    assert cuerpo["sesion"]["redireccion"].startswith("http://receptor.test/registro/paso-4?sesion_id=")

    # Una sola entrega, firmada, con el resultado y TODAS las imágenes como archivos.
    assert len(receptor.recibidas) == 1
    req = receptor.recibidas[0]
    assert req.headers["X-Huellas-Evento"] == "sesion.completada"
    assert entrega.verificar(proyecto["webhook_secreto"], req.headers["X-Huellas-Firma"], req.content)
    evento, archivos = partes(req)
    datos = evento["datos"]
    assert datos["sesion"]["estado"] == "completada" and datos["sesion"]["aprobada"] is True
    assert datos["sesion"]["referencia_externa"] == "cliente-123"
    assert datos["sesion"]["metadata"] == {"tramite": "alta"}
    assert datos["resultado"]["imagenes"] == {"captura": "captura.jpg", "mano_anotada": "mano_anotada.jpg"}
    assert datos["resultado"]["dedos"][0]["imagenes"]["realce_gabor"] == "indice_realce_gabor.png"
    assert len(archivos) == 2 + 4 * 5
    assert archivos["indice_realce_gabor.png"][:4] == b"\x89PNG"
    assert "base64" not in json.dumps(evento)

    # Confirmado por el proyecto: las huellas ya no están en memoria y no se escribió nada en disco.
    en_memoria = sesiones._sesiones[sesion["id"]]
    assert en_memoria.mejor_resultado is None and en_memoria.mejor_captura is None
    escritos = [p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()]
    assert escritos == ["config/proyectos.json"]
    assert not os.path.exists("data")

    estado = cliente.get(f"/api/v1/sesiones/{sesion['id']}", headers={"X-API-Key": proyecto["api_key"]}).json()
    assert estado["estado"] == "completada" and estado["entrega"]["ok"] is True and "resultado" not in estado


def test_si_el_proyecto_no_confirma_se_puede_reintentar(cliente, proyecto, receptor):
    receptor.codigo = 500
    sesion = crear_sesion(cliente, proyecto)
    cuerpo = enviar_intento(cliente, sesion).json()
    assert cuerpo["sesion"]["estado"] == "error_entrega" and cuerpo["sesion"]["redireccion"] is None
    assert sesiones._sesiones[sesion["id"]].mejor_resultado is not None  # aún en memoria para reintentar

    receptor.codigo = 200
    r = cliente.post(f"/api/v1/captura/{token_de(sesion)}/finalizar")
    assert r.status_code == 200 and r.json()["estado"] == "completada"
    assert len(receptor.recibidas) == 2
    assert sesiones._sesiones[sesion["id"]].mejor_resultado is None


def test_autenticacion(cliente, proyecto):
    assert cliente.post("/api/v1/sesiones", json={}).status_code == 401
    assert cliente.post("/api/v1/sesiones", json={}, headers={"X-API-Key": "hk_falsa"}).status_code == 401


def test_urls_y_webhook_obligatorio(cliente, proyecto):
    r = cliente.post("/api/v1/sesiones", json={"url_retorno": "https://sitio-malicioso.com/x"},
                     headers={"X-API-Key": proyecto["api_key"]})
    assert r.status_code == 422
    sin_webhook = proyectos.crear("Sin webhook")
    r = cliente.post("/api/v1/sesiones", json={}, headers={"X-API-Key": sin_webhook["api_key"]})
    assert r.status_code == 422 and "webhook_url" in r.json()["detail"]


def test_calidad_insuficiente_agota_intentos(cliente, proyecto, receptor):
    sesion = crear_sesion(cliente, proyecto, max_intentos=2)
    assert enviar_intento(cliente, sesion, crestas=False).json()["sesion"]["estado"] == "pendiente"
    assert receptor.recibidas == []
    segundo = enviar_intento(cliente, sesion, crestas=False).json()
    assert segundo["sesion"]["estado"] == "completada" and segundo["sesion"]["aprobada"] is False
    assert partes(receptor.recibidas[0])[0]["datos"]["sesion"]["aprobada"] is False
    assert enviar_intento(cliente, sesion).status_code == 409


def test_intentos_automaticos_no_agotan_la_sesion(cliente, proyecto, receptor, monkeypatch):
    sesion = crear_sesion(cliente, proyecto, max_intentos=1)
    for _ in range(3):
        r = enviar_intento(cliente, sesion, crestas=False, automatico=True).json()
        assert r["sesion"]["estado"] == "pendiente" and r["sesion"]["intentos"] == 0
    assert receptor.recibidas == []
    monkeypatch.setattr(sesiones, "MAX_INTENTOS_AUTO", 3)
    assert enviar_intento(cliente, sesion, crestas=False, automatico=True).status_code == 429
    # La mejor captura automática sigue disponible para "Usar la mejor captura".
    assert cliente.post(f"/api/v1/captura/{token_de(sesion)}/finalizar").json()["estado"] == "completada"


def test_intento_automatico_aprobado_finaliza(cliente, proyecto, receptor):
    sesion = crear_sesion(cliente, proyecto, calidad_minima=20)
    r = enviar_intento(cliente, sesion, automatico=True).json()
    assert r["intento_aprobado"] and r["sesion"]["estado"] == "completada" and r["sesion"]["intentos"] == 1


def test_finalizar_manual(cliente, proyecto):
    sesion = crear_sesion(cliente, proyecto, calidad_minima=95, max_intentos=5)
    token = token_de(sesion)
    assert cliente.post(f"/api/v1/captura/{token}/finalizar").status_code == 409
    enviar_intento(cliente, sesion, crestas=False)
    r = cliente.post(f"/api/v1/captura/{token}/finalizar")
    assert r.status_code == 200 and r.json()["estado"] == "completada"


def correr_mantenimiento() -> dict:
    async def correr():
        r = sesiones.mantenimiento()
        await asyncio.sleep(0.05)  # deja salir el aviso de expiración
        return r
    return asyncio.run(correr())


def test_expiracion_olvida_todo(cliente, proyecto, receptor):
    sesion = crear_sesion(cliente, proyecto, calidad_minima=95)
    enviar_intento(cliente, sesion, crestas=False)
    s = sesiones._sesiones[sesion["id"]]
    s.expira -= timedelta(hours=2)
    assert correr_mantenimiento()["expiradas"] == 1
    assert s.estado == "expirada" and s.mejor_resultado is None
    evento, archivos = partes(receptor.recibidas[-1])
    assert evento["evento"] == "sesion.expirada" and archivos == {}

    s.expira -= timedelta(hours=2)
    assert correr_mantenimiento()["olvidadas"] == 1
    r = cliente.get(f"/api/v1/sesiones/{sesion['id']}", headers={"X-API-Key": proyecto["api_key"]})
    assert r.status_code == 404


def test_cancelar(cliente, proyecto):
    sesion = crear_sesion(cliente, proyecto)
    r = cliente.post(f"/api/v1/sesiones/{sesion['id']}/cancelar", headers={"X-API-Key": proyecto["api_key"]})
    assert r.status_code == 200 and r.json()["estado"] == "cancelada"
    assert enviar_intento(cliente, sesion).status_code == 409
    assert cliente.get(f"/api/v1/captura/{token_de(sesion)}").json()["estado"] == "cancelada"


def test_firma_rechaza_alteraciones():
    cuerpo = b'{"a":1}'
    cab = entrega.cabecera_firma("whsec_x", cuerpo)
    assert entrega.verificar("whsec_x", cab, cuerpo)
    assert not entrega.verificar("whsec_x", cab, b'{"a":2}')
    assert not entrega.verificar("whsec_otro", cab, cuerpo)
    vieja = entrega.cabecera_firma("whsec_x", cuerpo, timestamp=int(time.time()) - 3600)
    assert not entrega.verificar("whsec_x", vieja, cuerpo)


def test_proyectos_en_archivo_sin_claves_en_claro(cliente, tmp_path):
    nuevo = proyectos.crear("Asistencia", "http://receptor.test/w")
    archivo = tmp_path / "config" / "proyectos.json"
    contenido = archivo.read_text()
    assert nuevo["api_key"] not in contenido and nuevo["webhook_secreto"] in contenido
    assert oct(archivo.stat().st_mode & 0o777) == "0o600"
    nueva_key = proyectos.rotar_api_key(nuevo["id"])
    assert cliente.get("/api/v1/sesiones", headers={"X-API-Key": nuevo["api_key"]}).status_code == 401
    assert cliente.get("/api/v1/sesiones", headers={"X-API-Key": nueva_key}).status_code == 200


def test_puente_con_qr(cliente, proyecto):
    clave = {"X-API-Key": proyecto["api_key"]}
    sesion = crear_sesion(cliente, proyecto)
    token = token_de(sesion)
    assert sesion["url_puente"].endswith(f"/puente/{token}")

    pagina = cliente.get(f"/puente/{token}")
    assert "frame-ancestors 'self' https://receptor.test" in pagina.headers["content-security-policy"]
    qr = cliente.get(f"/api/v1/captura/{token}/qr.svg")
    assert qr.headers["content-type"].startswith("image/svg+xml") and 'xmlns="http://www.w3.org/2000/svg"' in qr.text
    assert cliente.get(f"/api/v1/sesiones/{sesion['id']}/qr.svg", headers=clave).status_code == 200

    assert cliente.get(f"/api/v1/captura/{token}").json()["celular_conectado"] is False
    cliente.get(f"/api/v1/captura/{token}?modo=qr")
    assert cliente.get(f"/api/v1/captura/{token}").json()["celular_conectado"] is True
    enviar_intento(cliente, sesion)
    estado = cliente.get(f"/api/v1/captura/{token}").json()
    assert estado["estado"] == "completada" and estado["redireccion"].startswith("http://receptor.test/registro/paso-4")
    assert cliente.get(f"/api/v1/captura/{token}/qr.svg").status_code == 409
    assert cliente.get(f"/fin/{token}").status_code == 200
