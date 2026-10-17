# ============================================================
# WebCut 后端 Dockerfile
# 构建：docker build -t webcut-backend .
# 运行：docker run -p 8787:8787 --env-file ai-backend/.env webcut-backend
# 或用 docker-compose up -d（推荐，见 docker-compose.yml）
# ============================================================
FROM python:3.11-slim

WORKDIR /app

# 系统依赖（无额外需求，slim 即可）
# Python 依赖：qrcode+pypng（支付二维码）、cryptography（支付宝 RSA2 签名）
COPY ai-backend/requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

# 复制项目（后端 + 前端静态文件）
COPY ai-backend/ ai-backend/
COPY index.html entitlements-config.js vercel.json ./
COPY js/ js/
COPY api/ api/

# 运行时目录（云同步/支付数据/临时文件）
RUN mkdir -p ai-backend/cloud ai-backend/data ai-backend/tmp

# 非 root 用户运行（安全）
RUN useradd -m webcut && chown -R webcut:webcut /app
USER webcut

EXPOSE 8787

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/ai/health', timeout=3)" || exit 1

WORKDIR /app/ai-backend
CMD ["python", "-u", "server.py"]
