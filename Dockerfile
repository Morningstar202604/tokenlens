# TokenLens 容器化部署：本地透明代理 + 仪表盘
# 构建: docker build -t tokenlens:1.2.1 .
# 运行: docker run -d --name tokenlens -p 8787:8787 \
#         -v $HOME/.tokenlens:/root/.tokenlens tokenlens:1.2.1
FROM python:3.11-slim

# 时区与依赖缓存
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY tokenlens ./tokenlens
COPY pyproject.toml README.md LICENSE ./
RUN pip install --no-cache-dir .

# 健康检查：仪表盘 API 存活探测（镜像内无 curl，用 python 探测）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/stats?rng=today', timeout=4).status==200 else 1)"

EXPOSE 8787

# 数据目录：config.json 与 usage.db 持久化在 /root/.tokenlens
VOLUME ["/root/.tokenlens"]

CMD ["python", "-m", "tokenlens", "start", "--host", "0.0.0.0"]
