"""수요 예측 서비스(ML-Product)에서 시장 수요를 읽어 온다.

## 왜 에이전트가 부르는가

영업 대상을 고르려면 "어느 시장의 수요가 오르는가" 를 알아야 하는데, 그 답은
예측 서비스에 있다. 콘솔이 대신 부르고 결과를 넘겨줄 수도 있지만, 그러면
**무엇을 조사할지 고르는 판단이 화면으로 새어 나간다.** 이 에이전트의 존재
이유가 조사 순서와 도구 선택이므로, 수요 조회도 그 도구 중 하나로 둔다.

## 예측과 오차를 함께 가져온다

`/forecast` 는 숙소·날짜별 예측만 준다. 오차는 `/forecast/segments` 에 있다.
두 번 부르는 것이 번거로워 보이지만, **예측만 들고 오면 하류에서 신뢰도를
매길 수 없다.** 예측 서비스가 `low-demand` 응답에 `region_wape` 를 일부러
같이 담아 "낮은 예측 하나만으로는 행동할 수 없다" 를 스키마로 강제해 둔 것과
같은 이유다. 그 강제를 이 홉에서 떨어뜨리면 의미가 없어진다.

## 못 닿은 것과 수요가 없는 것은 다르다

서비스가 죽었을 때 빈 목록을 돌려주면 하류는 "이 시장에 수요가 없다" 로 읽고,
그 판단으로 영업 대상을 지운다. 그래서 닿지 못하면 **예외를 던진다.**
"""
from __future__ import annotations

import os

import httpx

#: 수요 예측 서비스 주소.
FORECAST_API_URL = os.getenv("FORECAST_API_URL", "http://127.0.0.1:8001")


class ForecastUnavailable(RuntimeError):
    """예측 서비스에 못 닿았다. **'수요가 없다' 와 다른 사실이다.**"""


class ForecastClient:
    """예측 서비스의 얇은 클라이언트. 해석하지 않고 그대로 옮긴다.

    지역 이름 정규화(``Seoul`` → ``서울``)는 **여기서 하지 않는다.**
    그 규칙은 이미 `analytics.acquisition.normalize_region` 에 있고, 두 벌로
    만들면 한쪽만 고쳤을 때 조용히 갈린다.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 5.0,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url or FORECAST_API_URL).rstrip("/")
        self.timeout = timeout
        #: 테스트에서 가짜 전송을 끼우는 구멍. **`_get` 을 통째로 갈아 끼우면
        #: 예외 변환 코드가 시험을 안 받는다** — 정작 거기가 지켜야 할 규칙이다.
        self.transport = transport

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                res = client.get(f"{self.base_url}{path}", params=params)
                res.raise_for_status()
                return res.json()
        except httpx.HTTPError as e:
            raise ForecastUnavailable(f"예측 서비스에 못 닿았다: {e}") from e

    def rows(self, region: str | None = None, limit: int = 2000) -> list[dict]:
        """숙소·날짜별 예측. 시장 단위로 접는 것은 호출자가 한다."""
        params: dict = {"limit": limit}
        if region:
            params["region"] = region
        return self._get("/forecast", params).get("rows", [])

    def wape_by_region(self) -> dict[str, float]:
        """지역별 예측 오차. **예측과 짝지어 다녀야 하는 값이다.**"""
        payload = self._get("/forecast/segments", {"by": "region"})
        return {r["key"]: r["wape"] for r in payload.get("rows", [])}

    def market_demand(self, region: str | None = None) -> dict:
        """영업이 쓰는 모양 — 예측 행과 지역별 오차를 한 번에.

        오차 조회가 실패해도 예측은 돌려준다. 오차는 신뢰도를 매기는 데 쓰이고,
        신뢰도는 하류에서 ``unknown`` 으로 떨어질 뿐이라 **판단을 멈출 이유가
        아니다.** 반대로 예측이 없으면 아무것도 할 수 없어 그때는 예외가 난다.
        """
        rows = self.rows(region=region, limit=2000)
        try:
            wape = self.wape_by_region()
        except ForecastUnavailable:
            wape = {}
        return {"rows": rows, "wape_by_region": wape, "count": len(rows)}


__all__ = ["FORECAST_API_URL", "ForecastClient", "ForecastUnavailable"]
