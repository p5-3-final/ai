# syntax=docker/dockerfile:1
# ai-gateway 이미지. 빌드 컨텍스트는 레포 루트이고, 분류 체계가 든 서비스 간 계약 폴더를 따로 넘긴다.
# CI는 service-contracts.lock 버전(scripts/fetch-service-contracts.sh), 로컬은 작업 공간의 계약(docs 레포 service-contracts/)이다.
#   docker build -f services/ai-gateway/Dockerfile \
#     --build-context vlm-contracts:docs=../../service-contracts -t vlm/ai-gateway .
# 마이그레이션은 앱보다 먼저 같은 이미지로 한 번 실행한다(Kubernetes Job·로컬 compose 단계):
#   docker run --rm vlm/ai-gateway python -m ai_gateway.db.migrate

FROM python:3.12-slim-trixie@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app

FROM base AS uv
COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /usr/local/bin/uv

FROM uv AS build
COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services
# 소스 경로를 가리키는 편집 설치가 아니라 가상환경 안에 복사해 넣어야 runtime 단계로 옮길 수 있다.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package ai-gateway
# torch wheel에 함께 들어오는 torch 자체 테스트·C++ 헤더·테스트 실행 파일은 실행에 쓰지 않는다(약 220MB, #21).
# bin의 torch_shm_manager는 멀티프로세싱 텐서 공유가 쓰므로 남긴다.
RUN torch_dir=/opt/venv/lib/python3.12/site-packages/torch \
    && rm -rf "$torch_dir/test" "$torch_dir/include" \
    && find "$torch_dir/bin" -maxdepth 1 -type f \( -name 'test_*' -o -name '*Test' \) -delete

# --build-context vlm-contracts:docs=<계약 폴더>로 받은 서비스 간 계약.
# 이미지처럼 태그를 붙여 받아야 hadolint가 출처를 알 수 있다.
FROM vlm-contracts:docs AS docs-contracts

FROM base AS runtime
# Kubernetes runAsNonRoot가 확인할 수 있도록 숫자 UID로 실행한다.
# 인코더 가중치 캐시(AI_GATEWAY_MODEL_CACHE_DIR 기본값). 가중치는 이미지에 넣지 않고 기동할 때 받는다
# (SPEC-011 NFR-6). 운영은 이 경로에 emptyDir을 붙인다.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app \
    && install -d -o 10001 -g 10001 /var/cache/vlm-models
COPY --from=build /opt/venv /opt/venv
# Prompt Registry(SPEC-009, SPEC-012). 프롬프트·질의 사전은 이 레포가 원본이고 분류 체계·출력 형식은 계약에서
# 가져와 합친다(docs ADR-0012).
COPY services/ai-gateway/registry/prompts /app/registry/prompts
COPY services/ai-gateway/registry/query /app/registry/query
COPY --from=docs-contracts taxonomy /app/registry/taxonomy
# 출력 형식 중 event.analyzed·track.analyzed·aggregator 결과 파일로 다른 레포에 나가는 것은 계약이
# 원본이다(SPEC-020, SPEC-024, SPEC-027).
COPY --from=docs-contracts \
    events/_vlm-analysis.schema.json \
    events/_track-attribute.schema.json \
    events/_object-answer.schema.json \
    events/_source-summary.schema.json \
    /app/registry/events/
COPY services/ai-gateway/profiles /app/profiles
ENV AI_GATEWAY_REGISTRY_DIR=/app/registry \
    AI_GATEWAY_PROFILE_PATH=/app/profiles/local.toml
USER 10001:10001
EXPOSE 8000
# 접근 로그는 구조화 로그와 형식이 달라 끄고, 호출마다 "analyze completed"·"parse completed"를 남긴다.
CMD ["uvicorn", "--factory", "ai_gateway.main:production_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
