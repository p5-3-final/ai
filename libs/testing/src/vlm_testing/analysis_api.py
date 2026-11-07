"""analysis-api 내부 읽기 API 자리 표시 서버(SPEC-027 plan 5절 B안).

analysis-api가 아직 없는 로컬 compose에서 aggregator를 끝까지 돌려 보기 위한 도구다. 시나리오 JSON
하나가 job 하나의 이벤트·트랙·머문 구간·구역과 직전 영상 끝 상태를 담는다. 경로의 sourceId·jobId는
보지 않고 같은 시나리오를 돌려준다. 응답 모양은 계약(`analysis-service.yaml` 내부 API)과 같게
시나리오 작성자가 맞춘다. 서버는 내용을 검사하지 않는다.

    ANALYSIS_API_SCENARIO=scenario.json uv run python -m vlm_testing.analysis_api
"""

from pathlib import Path
from typing import Annotated
from uuid import UUID

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings, SettingsConfigDict

type Item = dict[str, object]

# 계약 limit 상한
MAX_LIMIT = 1000


class Scenario(BaseModel):
    """job 하나의 읽기 응답. 키는 계약과 같은 camelCase다."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    events: list[Item] = []
    tracks: list[Item] = []
    presences: list[Item] = []
    zones: list[Item] = []
    # 직전 영상 sourceId → `getSourceFinalState` 응답. 없는 영상은 404다.
    final_states: dict[UUID, Item] = {}


class PlaceholderSettings(BaseSettings):
    """환경변수 `ANALYSIS_API_SCENARIO`, `ANALYSIS_API_HOST`, `ANALYSIS_API_PORT`."""

    model_config = SettingsConfigDict(env_prefix="ANALYSIS_API_", frozen=True, extra="ignore")

    scenario: Path
    # 컨테이너에서 띄울 때는 다른 컨테이너가 붙도록 0.0.0.0으로 바꾼다.
    host: str = "127.0.0.1"
    # analysis-api 내부 포트(docs 서비스 카탈로그)
    port: int = 8085


def _page(items: list[Item], cursor: str | None, limit: int) -> Item:
    # 커서는 다음 항목의 위치다. 실제 서버의 불투명 커서와 달리 숫자지만 클라이언트는
    # 받은 값을 그대로 되돌려 보내므로 차이가 없다.
    start = int(cursor) if cursor else 0
    end = start + limit
    return {"items": items[start:end], "nextCursor": str(end) if end < len(items) else None}


def _not_found(detail: str) -> JSONResponse:
    body = {"type": "about:blank", "title": "Not Found", "status": 404, "detail": detail}
    return JSONResponse(body, status_code=404, media_type="application/problem+json")


def create_app(scenario: Scenario) -> FastAPI:
    """시나리오를 돌려주는 앱을 만든다."""
    app = FastAPI(title="analysis-api placeholder")
    limit_query = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
    jobs = "/internal/v1/sources/{source_id}/jobs/{job_id}"

    @app.get(f"{jobs}/events")
    def list_job_events(cursor: str | None = None, limit: limit_query = MAX_LIMIT) -> Item:
        return _page(scenario.events, cursor, limit)

    @app.get(f"{jobs}/tracks")
    def list_job_tracks(cursor: str | None = None, limit: limit_query = MAX_LIMIT) -> Item:
        return _page(scenario.tracks, cursor, limit)

    @app.get(f"{jobs}/zone-presences")
    def list_job_zone_presences(cursor: str | None = None, limit: limit_query = MAX_LIMIT) -> Item:
        return _page(scenario.presences, cursor, limit)

    @app.get("/internal/v1/sources/{source_id}/zones")
    def list_source_zones(source_id: UUID) -> Item:
        return {"items": scenario.zones}

    @app.get("/internal/v1/sources/{source_id}/final-state", response_model=None)
    def get_source_final_state(source_id: UUID) -> Item | JSONResponse:
        final = scenario.final_states.get(source_id)
        return final if final is not None else _not_found(f"no final state for {source_id}")

    return app


def main() -> None:
    settings = PlaceholderSettings()
    scenario = Scenario.model_validate_json(settings.scenario.read_text(encoding="utf-8"))
    uvicorn.run(create_app(scenario), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
