FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates/ templates/
COPY static/ static/
EXPOSE 8000
# -w 1: un solo worker a propósito. El poller de apagones corre un hilo en
# background dentro del proceso de la app; con más de un worker se duplicaría
# y mandaría el WhatsApp de cada apagón dos veces.
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "1", "--timeout", "60", "app:app"]
