"""수요 예측 조회 도구.

여기서 고정하는 것은 예측값의 정확도가 아니라 **못 닿은 것과 수요가 없는 것을
가르는가**이다. 그 둘이 같은 응답으로 내려가면 하류는 서비스 장애를 "이 시장은
죽었다" 로 읽고 영업 대상을 지운다 — 조용히, 에러 하나 없이.
"""
from __future__ import annotations

import httpx
import pytest

from app.agent.forecast import ForecastClient, ForecastUnavailable
from app.agent.tools import TOOL_REGISTRY, ReadTools, Risk

FORECAST_ROWS = {
    "rows": [
        {"property_id": "P001", "region": "Jeju", "property_type": "PENSION",
         "stay_date": "2025-07-01", "as_of": "2025-06-24",
         "predicted": 2.4, "actual": 3.0},
        {"property_id": "P002", "region": "Seoul", "property_type": "HOTEL",
         "stay_date": "2025-07-01", "as_of": "2025-06-24",
         "predicted": 5.1, "actual": 5.0},
    ]
}
SEGMENT_ROWS = {
    "rows": [
        {"key": "Jeju", "wape": 0.3448, "mae": 1.0, "rmse": 1.4,
         "zero_ratio": 0.1, "n": 300},
        {"key": "Seoul", "wape": 0.2758, "mae": 0.9, "rmse": 1.2,
         "zero_ratio": 0.1, "n": 360},
    ]
}


def client_with(handler) -> ForecastClient:
    """가짜 전송을 끼운 진짜 클라이언트.

    ``_get`` 을 갈아 끼우지 않는 것이 중요하다 — 그러면 HTTP 오류를
    ``ForecastUnavailable`` 로 바꾸는 코드가 시험을 안 받는데, 정작 이 파일이
    지키려는 규칙이 거기 있다.
    """
    return ForecastClient(base_url="http://forecast.test",
                          transport=httpx.MockTransport(handler))


def ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/forecast":
        return httpx.Response(200, json=FORECAST_ROWS)
    if request.url.path == "/forecast/segments":
        return httpx.Response(200, json=SEGMENT_ROWS)
    return httpx.Response(404)


def down_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503)


# ── 클라이언트 ───────────────────────────────────────────────

def test_예측_행을_그대로_옮긴다():
    rows = client_with(ok_handler).rows()
    assert len(rows) == 2
    assert rows[0]["region"] == "Jeju"        # 정규화는 여기서 하지 않는다


def test_지역_이름을_여기서_바꾸지_않는다():
    """정규화 규칙은 analytics.acquisition 에 이미 있다. 두 벌로 만들면
    한쪽만 고쳤을 때 조용히 갈린다."""
    rows = client_with(ok_handler).rows()
    assert {r["region"] for r in rows} == {"Jeju", "Seoul"}


def test_예측과_오차를_함께_가져온다():
    """예측만 들고 오면 하류에서 신뢰도를 매길 수 없다."""
    payload = client_with(ok_handler).market_demand()
    assert payload["count"] == 2
    assert payload["wape_by_region"]["Jeju"] == 0.3448


def test_서비스가_죽으면_빈_목록이_아니라_예외다():
    with pytest.raises(ForecastUnavailable):
        client_with(down_handler).rows()


def test_오차_조회만_실패하면_예측은_살린다():
    """오차는 신뢰도를 unknown 으로 떨어뜨릴 뿐 판단을 멈출 이유가 아니다."""
    def partial(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/forecast":
            return httpx.Response(200, json=FORECAST_ROWS)
        return httpx.Response(500)

    payload = client_with(partial).market_demand()
    assert payload["count"] == 2
    assert payload["wape_by_region"] == {}


# ── 도구 ─────────────────────────────────────────────────────

class FakeStore:
    """도구가 store 를 만지지 않는 경로라 빈 껍데기면 충분하다."""


def test_도구가_수요와_오차를_돌려준다():
    tools = ReadTools(FakeStore(), forecast_client=client_with(ok_handler))
    result = tools.get_market_demand()

    assert result.ok
    assert result.data["count"] == 2
    assert result.data["wape_by_region"]["Seoul"] == 0.2758


def test_못_닿은_것은_실패로_내려간다():
    """빈 목록으로 내려보내면 하류가 "수요가 없다" 로 읽는다."""
    tools = ReadTools(FakeStore(), forecast_client=client_with(down_handler))
    result = tools.get_market_demand()

    assert not result.ok
    assert "못 닿" in (result.error or "")


def test_빈_예측도_성공이_아니다():
    def empty(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/forecast":
            return httpx.Response(200, json={"rows": []})
        return httpx.Response(200, json=SEGMENT_ROWS)

    result = ReadTools(FakeStore(), forecast_client=client_with(empty)).get_market_demand()
    assert not result.ok


def test_수요_조회는_승인이_필요없는_읽기_도구다():
    from app.agent.tools import requires_confirmation

    assert TOOL_REGISTRY["get_market_demand"] is Risk.LOW
    assert not requires_confirmation("get_market_demand")
