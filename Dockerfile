FROM python:3.14.8-alpine3.24@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72 AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements/ requirements/
RUN python -m pip install --require-hashes -r requirements/build.lock \
    && python -m pip download --only-binary=:all: --require-hashes \
       -r requirements/runtime.lock --dest /wheels
COPY pyproject.toml README.md ./
COPY src/ src/
RUN python -m build --wheel --no-isolation --outdir /wheels

FROM builder AS test
RUN python -m pip install --only-binary=:all: --require-hashes -r requirements/development.lock \
    && python -m pip install --only-binary=:all: --require-hashes -r requirements/research.lock \
    && python -m pip install --no-deps --no-index /wheels/aca_platform-0.4.22-py3-none-any.whl \
    && python -m pip check \
    && addgroup -g 10001 aca && adduser -D -u 10001 -G aca -s /sbin/nologin aca
COPY tests/ tests/
COPY scripts/ scripts/
COPY configs/ configs/
ENV PYTHONDONTWRITEBYTECODE=1 HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis
USER 10001:10001
CMD ["python", "scripts/linux_tests.py"]

FROM python:3.14.8-alpine3.24@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 ACA_CONFIG=/app/configs/paper.toml
WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements/runtime.lock /tmp/runtime.lock
RUN python -m pip install --no-index --find-links=/wheels --require-hashes -r /tmp/runtime.lock \
    && python -m pip install --no-deps --no-index /wheels/aca_platform-0.4.22-py3-none-any.whl \
    && python -m pip uninstall --yes pip \
    && rm -rf /wheels /tmp/runtime.lock /usr/local/lib/python3.14/ensurepip \
    && addgroup -g 10001 aca && adduser -D -u 10001 -G aca -s /sbin/nologin aca
COPY configs/paper.toml /app/configs/paper.toml
USER 10001:10001
HEALTHCHECK --interval=5s --timeout=4s --start-period=10s --retries=6 CMD ["aca", "health"]
CMD ["aca", "serve"]
