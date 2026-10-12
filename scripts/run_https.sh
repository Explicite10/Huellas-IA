#!/usr/bin/env bash
# Levanta la API con HTTPS para poder usar la cámara desde un celular en la misma red.
# Los navegadores solo permiten la cámara en https:// o en localhost.
set -euo pipefail
cd "$(dirname "$0")/.."
IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
PUERTO=${PUERTO:-8443}
mkdir -p certs
if [ ! -f certs/cert.pem ] || ! grep -q "$IP" certs/.ip 2>/dev/null; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout certs/key.pem -out certs/cert.pem -subj "/CN=huellas-local" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:${IP}" >/dev/null 2>&1
  echo "$IP" > certs/.ip
fi
echo "Abre en el celular: https://${IP}:${PUERTO}  (acepta el aviso de certificado autofirmado)"
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PUERTO" \
  --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem
