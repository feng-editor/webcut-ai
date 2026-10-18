FROM python:3.11-slim
WORKDIR /app
COPY ai-backend/ ai-backend/
RUN pip install --no-cache-dir flask flask-cors requests urllib3
WORKDIR /app/ai-backend
EXPOSE 8787
CMD ["python", "-u", "server_flask.py"]
