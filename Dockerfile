# --- Frontend Builder Stage ---
FROM node:22-alpine AS frontend-builder

# 配置国内 npm 源为淘宝镜像
RUN npm config set registry https://registry.npmmirror.com && \
    corepack enable pnpm && \
    pnpm config set registry https://registry.npmmirror.com

# Copy frontend files
COPY frontend /app/frontend

# Build frontend
WORKDIR /app/frontend
RUN corepack enable pnpm && \
    pnpm install && \
    pnpm install dayjs && \
    pnpm build

# --- Python Dependencies Stage ---
FROM python:3.10.17-slim-bullseye AS python-deps

# Set custom APT sources
RUN sed -i 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' /etc/apt/sources.list && \
    sed -i 's|http://security.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list


# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY backend/requirements.txt /requirements.txt

# 设置国内清华 pip 源
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# Install Python dependencies
RUN pip install --no-cache-dir -r /requirements.txt

# --- Final Stage ---
FROM python:3.10.17-slim-bullseye

# Environment variables
ENV HOST=0.0.0.0 \
    FLASK_PORT=5000 \
    WS_PORT=8765 \
    FRONTEND_PORT=3000 \
    TZ=Asia/Shanghai \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Set custom APT sources
RUN sed -i 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' /etc/apt/sources.list && \
    sed -i 's|http://security.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list


# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    curl \
    bash \
    && rm -rf /var/lib/apt/lists/*


# Install Caddy using binary download
#RUN curl -L "https://github.com/caddyserver/caddy/releases/download/v2.7.6/caddy_2.7.6_linux_amd64.tar.gz" -o caddy.tar.gz \
RUN curl -L "https://webstatic.wj2015.com/caddy_2.7.6_linux_amd64.tar.gz" -o caddy.tar.gz \
    && tar -xzf caddy.tar.gz \
    && mv caddy /usr/local/bin/ \
    && chmod +x /usr/local/bin/caddy \
    && rm caddy.tar.gz

# Copy Python packages from python-deps stage
COPY --from=python-deps /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=python-deps /usr/local/bin /usr/local/bin

# Copy frontend build from frontend-builder stage
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

# Copy backend files
COPY backend /app/backend
COPY Caddyfile /app/

# Copy and set permissions for startup script
COPY docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

# Set working directory
WORKDIR /app

# Expose port
EXPOSE 80

# Startup command
ENTRYPOINT ["/bin/bash", "/app/docker-entrypoint.sh"]
