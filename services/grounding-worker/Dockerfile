# syntax=docker/dockerfile:1
# grounding-worker 이미지. 빌드 컨텍스트는 레포 루트다(workspace의 libs를 함께 쓴다).
#   docker build -f services/grounding-worker/Dockerfile -t vlm/grounding-worker .
#   docker build -f services/grounding-worker/Dockerfile --target test -t vlm/grounding-worker-test .

FROM python:3.12-slim-trixie@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS base
# 샘플 프레임 추출과 주석 클립 인코딩은 시스템 ffmpeg·ffprobe를 부른다(SPEC-026 plan D-2).
# drawtext 라벨에는 글꼴 파일이 필요해 DejaVu를 넣는다. 테스트도 이 이미지에서 돌려 운영과 같은
# 버전으로 검증한다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg=7:7.1.5-0+deb13u1 \
        fonts-dejavu-core=2.37-8 \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app

FROM base AS uv
COPY --from=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 /uv /usr/local/bin/uv

# 개발 의존성을 포함한 테스트 이미지. 소스는 실행할 때 /app에 마운트한다(scripts/test-in-docker.sh).
# testcontainers가 Docker 소켓으로 Kafka·SeaweedFS를 띄우므로 root로 둔다. 배포하지 않는 단계다.
FROM uv AS test
COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services
# evals도 workspace 멤버라 --all-packages가 그 pyproject를 찾는다(SPEC-030).
COPY evals evals
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --all-packages

FROM uv AS build
COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services
# 소스 경로를 가리키는 편집 설치가 아니라 가상환경 안에 복사해 넣어야 runtime 단계로 옮길 수 있다.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package grounding-worker

FROM base AS runtime
# Kubernetes runAsNonRoot가 확인할 수 있도록 숫자 UID로 실행한다.
# 클립을 잠깐 쓰는 작업 폴더(GROUNDING_WORKER_WORK_DIR 기본값). 운영은 emptyDir을 붙인다.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app \
    && install -d -o 10001 -g 10001 /var/tmp/grounding
COPY --from=build /opt/venv /opt/venv
COPY services/grounding-worker/profiles /app/profiles
# 실제 탐지기 프로필(local.toml)은 탐지기 라이브러리를 정한 뒤 더한다(tasks T027).
ENV GROUNDING_WORKER_PROFILE_PATH=/app/profiles/local-mock.toml \
    GROUNDING_WORKER_WORK_DIR=/var/tmp/grounding
USER 10001:10001
# health 전용 포트(docs 서비스 카탈로그 "포트 규칙")
EXPOSE 8000
ENTRYPOINT ["python", "-m", "grounding_worker"]
