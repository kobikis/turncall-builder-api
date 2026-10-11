# The one image every Agent Backend runs on Kubernetes (ADR-0025): the
# scaffold's requirements, baked in, so a backend starts without a build or a
# trip to PyPI. Its code arrives as a ConfigMap. Keep this list equal to the
# scaffold's requirements.txt — tests/test_kube_runner.py checks it.
FROM python:3.12-slim
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27" "tzdata>=2024.1" \
    && useradd --uid 10001 --create-home app
USER app
