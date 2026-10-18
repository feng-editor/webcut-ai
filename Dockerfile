# ============================================================
# WebCut 后端 Dockerfile
# 构建：docker build -t webcut-backend .
# 运行：docker run -p 8787:8787 --env-file ai-backend/.env webcut-backend
# 或用 docker-compose up -d（推荐，见 docker-compose.yml）
# ============================================================
FROM python:3.11-slim

WORKDIR /app

COPY ai-backend/requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY ai-backend/ ai-backend/
COPY index.html entitlements-config.js vercel.json netlify.toml ./
COPY js/ js/
COPY api/ api/

RUN mkdir -p ai-backend/cloud ai-backend/data ai-backend/tmp

RUN useradd -m webcut && chown -R webcut:webcut /app
USER webcut

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/ai/health', timeout=3)" || exit 1

WORKDIR /app/ai-backend
CMD ["python", "-u", "server_flask.py"]
