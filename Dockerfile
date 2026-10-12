FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Sin almacenamiento: solo se monta la configuración de proyectos (API keys en hash y secretos de firma).
ENV HUELLAS_PROYECTOS=/config/proyectos.json HUELLAS_DEMO=0
VOLUME /config
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
