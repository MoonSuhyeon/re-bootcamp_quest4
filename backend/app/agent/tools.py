"""Agent Tool — 읽기와 쓰기를 분리한다.

권한·감사·승인 게이트를 **도구 단위**로 걸기 위해서다.
하나의 도구가 조회와 변경을 겸하면 통제 지점이 사라진다.

쓰기 도구는 멱등성 키 없이 호출할 수 없다.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from enum import Enum

from app.domain import (
    Booking, BookingStatus, DuplicateRequest, PaymentGatewayError, Store,
)


class Risk(str, Enum):
    LOW = "LOW"        # 조회 — 자동 실행
    MEDIUM = "MEDIUM"  # 계산 — 자동 실행(안내만)
    HIGH = "HIGH"      # 상태 변경 — 고객 확인 필수


@dataclass
class ToolResult:
    ok: bool
    data: dict
    error: str | None = None


class ToolError(Exception):
    pass


def idempotency_key(booking_id: str, action: str, request_id: str) -> str:
    raw = f"{booking_id}:{action}:{request_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ============================================================ Read Tools
class ReadTools:
    """상태를 바꾸지 않는다. 자동 실행해도 안전하다."""

    risk = Risk.LOW

    def __init__(self, store: Store, today: date | None = None,
                 policy_retriever=None, forecast_client=None):
        self.store = store
        self.today = today or date.today()
        # 정책 조회는 RAG-Marketing 의 검색 코어를 재사용한다.
        # 주입하지 않으면 지연 생성한다(색인 비용을 필요할 때만 낸다).
        self._policy_retriever = policy_retriever
        # 수요 예측은 별도 서비스(ML-Product)에 있다. 같은 지연 생성 패턴 —
        # 예측을 안 쓰는 상담 경로에서 HTTP 클라이언트를 만들 이유가 없다.
        self._forecast_client = forecast_client

    def get_booking(self, booking_id: str) -> ToolResult:
        b = self.store.get_booking(booking_id)
        if b is None:
            return ToolResult(False, {}, f"예약 {booking_id} 을 찾을 수 없다")
        return ToolResult(True, {
            "booking_id": b.booking_id, "status": b.status.value,
            "property_id": b.property_id, "amount": b.amount,
            "check_in": b.check_in.isoformat(),
            "days_until_check_in": b.days_until_check_in(self.today),
            "refund_status": b.refund_status.value,
        })

    def get_property(self, property_id: str) -> ToolResult:
        p = self.store.get_property(property_id)
        if p is None:
            return ToolResult(False, {}, f"숙소 {property_id} 을 찾을 수 없다")
        return ToolResult(True, {"property_id": p.property_id, "name": p.name,
                                 "region": p.region})

    @property
    def policy_retriever(self):
        if self._policy_retriever is None:
            from app.agent.policy_rag import PolicyRetriever
            self._policy_retriever = PolicyRetriever(self.store)
        return self._policy_retriever

    def get_cancellation_policy(self, property_id: str) -> ToolResult:
        """정책 조회 — 문서 검색으로 찾는다. **근거가 약하면 추측하지 않고 실패한다.**

        검색은 언제나 무언가를 돌려주므로, 돌려준 것을 근거로 써도 되는지
        따로 판정한다. 기권하면 그래프가 에스컬레이션으로 빠진다.
        """
        found = self.policy_retriever.lookup(property_id)
        if not found:
            return ToolResult(False, {}, found.reason)
        pol = found.policy
        return ToolResult(True, {
            "policy_id": pol.policy_id, "name": pol.name,
            "description": pol.describe(),
            "tiers": pol.tiers,
            "retrieval_score": round(found.top_score, 6),
        })

    @property
    def forecast_client(self):
        if self._forecast_client is None:
            from app.agent.forecast import ForecastClient
            self._forecast_client = ForecastClient()
        return self._forecast_client

    def get_market_demand(self, region: str | None = None) -> ToolResult:
        """시장 수요 조회 — **예측과 그 오차를 함께 가져온다.**

        서비스에 못 닿으면 빈 목록이 아니라 실패를 돌려준다. 빈 목록으로
        내려보내면 하류가 "이 시장에 수요가 없다" 로 읽고 그 판단으로 영업
        대상을 지운다 — 서비스가 잠깐 죽은 것과 시장이 죽은 것은 다르다.
        """
        from app.agent.forecast import ForecastUnavailable
        try:
            payload = self.forecast_client.market_demand(region=region)
        except ForecastUnavailable as e:
            return ToolResult(False, {}, str(e))

        if not payload["rows"]:
            return ToolResult(False, {"count": 0},
                              "예측 행이 비어 있다 — 조회 조건을 확인해야 한다")
        return ToolResult(True, payload)

    def calculate_refund(self, booking_id: str) -> ToolResult:
        """환불 금액 계산. 안내만 하며 상태를 바꾸지 않는다."""
        b = self.store.get_booking(booking_id)
        if b is None:
            return ToolResult(False, {}, f"예약 {booking_id} 을 찾을 수 없다")

        # 저장소가 **권위 있는 금액**을 안다면 그것을 쓴다.
        #
        # 실제 예약은 예약 서비스가 소유하고, 환불액도 거기서 정한다. 여기서
        # 같은 산수를 한 번 더 하면 두 곳이 각자 계산하게 되고, 한쪽 구간만
        # 고쳤을 때 **설명과 집행이 조용히 갈린다.** 물어볼 수 있으면 묻는다.
        quote = getattr(self.store, "refund_quote", None)
        if callable(quote):
            q = quote(booking_id)
            if q is None:
                return ToolResult(False, {}, "환불 금액을 확인할 수 없다")
            return ToolResult(True, {"booking_id": booking_id, **q})

        found = self.policy_retriever.lookup(b.property_id)
        if not found:
            return ToolResult(False, {},
                              f"취소 정책을 확인할 수 없어 환불 금액을 계산할 수 없다 ({found.reason})")
        pol = found.policy

        days = b.days_until_check_in(self.today)
        ratio = pol.refund_ratio(days)
        amount = int(b.amount * ratio)
        return ToolResult(True, {
            "booking_id": booking_id, "original_amount": b.amount,
            "days_until_check_in": days, "refund_ratio": ratio,
            "refund_amount": amount, "policy": pol.describe(),
        })


# =========================================================== Write Tools
class WriteTools:
    """상태를 바꾼다. 고객 확인 없이 실행되어서는 안 된다."""

    risk = Risk.HIGH

    def __init__(self, store: Store):
        self.store = store

    def cancel_and_refund(self, booking_id: str, amount: int, key: str) -> ToolResult:
        """취소와 환불을 **함께** 처리한다.

        환불이 실패하면 취소를 되돌린다(보상 트랜잭션).
        되돌리지도 못하면 상태를 그대로 두고 에스컬레이션 신호를 낸다.
        """
        # 1) 멱등성 키 선점 — 중복이면 저장된 결과를 그대로 돌려준다
        try:
            self.store.claim(key)
        except DuplicateRequest as e:
            stored = self.store.stored_result(key)
            if stored:
                return ToolResult(True, {**stored, "idempotent_replay": True})
            return ToolResult(False, {}, "동일 요청이 처리 중이다")

        # 2) 취소
        self.store.cancel(booking_id)

        # 3) 환불 — 실패하면 보상
        try:
            self.store.refund(booking_id, amount)
        except PaymentGatewayError as pg:
            try:
                self.store.restore(booking_id)
                self.store.release(key)
                return ToolResult(False, {"compensated": True},
                                  f"환불 실패로 취소를 되돌렸다: {pg}")
            except PaymentGatewayError as comp:
                # 부분 처리 상태가 남았다. 사람이 개입해야 한다.
                return ToolResult(False, {"compensated": False, "needs_human": True},
                                  f"환불 실패({pg}) 후 보상도 실패({comp})")

        result = {"booking_id": booking_id, "refund_amount": amount,
                  "status": BookingStatus.CANCELLED.value}
        self.store.complete(key, result)
        return ToolResult(True, result)


# ============================================================== 도구 목록
TOOL_REGISTRY = {
    "get_booking": Risk.LOW,
    "get_property": Risk.LOW,
    "get_cancellation_policy": Risk.LOW,
    "get_market_demand": Risk.LOW,
    "calculate_refund": Risk.MEDIUM,
    "cancel_and_refund": Risk.HIGH,
}


def requires_confirmation(tool_name: str) -> bool:
    return TOOL_REGISTRY.get(tool_name) is Risk.HIGH


__all__ = [
    "ReadTools", "Risk", "TOOL_REGISTRY", "ToolError", "ToolResult",
    "WriteTools", "idempotency_key", "requires_confirmation",
]
