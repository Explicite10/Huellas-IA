#!/usr/bin/env bash
# Prueba completa desde el celular: servicio de huellas (HTTPS, puerto 8443) + proyecto de ejemplo (puerto 8100).
# Uso: ./scripts/demo_celular.sh      (Ctrl+C detiene ambos)
set -euo pipefail
cd "$(dirname "$0")/.."

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I | awk '{print $1}')
SERVICIO="https://${IP}:8443"
PROYECTO="http://${IP}:8100"
CLAVES=config/proyecto_demo_celular.json   # claves en claro, solo para esta demo local

# Solo para esta prueba local: acepta URLs http:// del proyecto de ejemplo.
export HUELLAS_PERMITIR_HTTP=1

mkdir -p config
if [ ! -f "$CLAVES" ]; then
  echo "Dando de alta el proyecto de ejemplo…"
  .venv/bin/python scripts/proyectos.py crear "Proyecto demo" 2>/dev/null | sed '/^$/,$d' > "$CLAVES"
fi
API_KEY=$(python3 -c "import json;print(json.load(open('$CLAVES'))['api_key'])")
SECRETO=$(python3 -c "import json;print(json.load(open('$CLAVES'))['webhook_secreto'])")

# Certificado autofirmado con la IP actual (los navegadores exigen HTTPS para la cámara).
mkdir -p certs
if [ ! -f certs/cert.pem ] || ! grep -qx "$IP" certs/.ip 2>/dev/null; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout certs/key.pem -out certs/cert.pem \
    -subj "/CN=huellas-local" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:${IP}" >/dev/null 2>&1
  echo "$IP" > certs/.ip
fi

trap 'kill $(jobs -p) 2>/dev/null' EXIT
HUELLAS_URL_PUBLICA="$SERVICIO" .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8443 \
  --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem --log-level warning &
HUELLAS_URL="https://localhost:8443" HUELLAS_VERIFICAR_TLS=0 HUELLAS_API_KEY="$API_KEY" \
  HUELLAS_WEBHOOK_SECRETO="$SECRETO" PROYECTO_URL="$PROYECTO" \
  .venv/bin/uvicorn ejemplos.proyecto_demo:app --host 0.0.0.0 --port 8100 --log-level warning &

sleep 4
cat <<EOF

  Listo. En el celular (mismo Wi-Fi que esta Mac):

  1) Abre primero  ${SERVICIO}/health
     y acepta el aviso del certificado (Mostrar detalles → visitar este sitio web).
  2) Luego abre    ${PROYECTO}
     y pulsa "Registrar huellas".

  Las huellas que guarda el proyecto quedan en: ejemplos/datos_proyecto/
  Ctrl+C para detener todo.

EOF
wait
