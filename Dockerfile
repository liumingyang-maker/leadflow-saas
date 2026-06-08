# LeadFlow SaaS 生产镜像
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

# 构建期可能需要的系统依赖（curl_cffi / Levenshtein 等极少数包需要编译时用）
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用缓存
COPY requirements.txt .
RUN pip install -r requirements.txt

# 再拷代码（.dockerignore 已排除数据/密钥/本地文件）
COPY . .

# 数据目录（挂持久卷到这里：数据库、租户数据都在这）
RUN mkdir -p /data

EXPOSE 8080
CMD ["python", "serve.py"]
