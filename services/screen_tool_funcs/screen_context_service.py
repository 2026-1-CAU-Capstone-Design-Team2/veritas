from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .capture.ocr_engine import OcrEngine
from .capture.powerpoint_com import PowerPointComReader
from .capture.screen_capture import ScreenCapture
from .capture.text_extraction_targets import is_text_extraction_target
from .capture.ui_automation import UiAutomationReader
from .capture.window_context import WindowContextReader
from .core.content_filter import ContentFilter
from .core.models import (
    AppTextResult,
    InterventionDecision,
    OcrResult,
    ScreenContextEvent,
    UiAutomationResult,
)
from .core.store import ScreenContextStore
from .intervention.caret_continuation import CaretContinuationEngine
from .intervention.intervention_detector import InterventionDetector
from .intervention.intervention_dispatcher import InterventionDispatcher
from .intervention.llm_router import ScenarioRouter
from .intervention.scenario_scheduler import ScenarioScheduler
from .scenario import (
    AcronymIntroducedScenario,
    BlankDocumentStartScenario,
    CitationMissingScenario,
    CodeBlockPresentScenario,
    CopyPasteGrowthScenario,
    FactualClaimMadeScenario,
    HeadingAddedScenario,
    IdleAfterWritingScenario,
    LargeDeletionScenario,
    LongParagraphWrittenScenario,
    LongStaticReviewScenario,
    ManyQuestionMarksScenario,
    NumberedListGrowthScenario,
    OutlinePhaseScenario,
    ParagraphChurnScenario,
    QuoteInsertedScenario,
    RepeatedPhraseInParagraphScenario,
    ScatteredEditsScenario,
    TodoMarkerPresentScenario,
    TransitionWordOveruseScenario,
    UndoCycleDetectedScenario,
    WeakModifierOveruseScenario,
    WholeDocumentReviewScenario,
)


"""
producer: ScreenContextService 폴링 스레드 -> decide() -> dispatcher.dispatch() -> store.enqueue_intervention() (디스크 intervention_queue.json에 append)

consumer: ChatAgent._screen_intervention_loop — 별도 스레드. consume(limit=1) -> answer_screen_intervention()에서 ~30초 블로킹 LLM 호출 -> 반복
"""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# feedback action → 발화 페이스 outcome. 수락 계열은 더 자주, 거절 계열은 더 드물게.
_CARD_OUTCOME_BY_ACTION: dict[str, str] = {
    "copy": "accept",
    "like": "accept",
    "retry": "retry",
    "red_reject": "reject",
    "reject": "reject",
    "dislike": "reject",
    "wrong_anchor": "reject",
    "timeout": "ignore",
}


class UnresolvedCardGate:
    """단일 슬롯 '미해결 카드' 게이트.

    외부 앱 suggestion 카드가 화면에 떠 있는데 사용자가 아직 반응(복사/거절/
    다시/위치다름)하지 않은 동안, 캡처 루프가 새 개입을 스케줄하지 못하게 막는다.
    이전에는 카드 표시 직후부터 다음 발화가 가능해서, 사용자가 한 단락을 고치고
    잠시 멈춘 사이 5~6개의 카드가 쌓였다 — "사용자 반응(또는 만료)이 다음 카드의
    페이스를 결정한다"가 이 게이트의 원칙.

    수명주기:
    - ``mark_shown``  — ChatAgent 답변 콜백의 첫 non-empty 청크 시점. 생성 중에는
      큐 점유(`_intervention_pipeline_busy`)가 막고 있으므로 이 시점 마킹으로
      빈틈이 없다. 빈 답변/스킵된 개입은 마킹되지 않아 헛 quiet-period가 없다.
    - ``resolve``     — feedback HTTP 경로에서 호출 (pd_* / legacy id 모두 매칭).
    - 자동 만료       — ``resolve_timeout_sec`` 동안 무반응이면 무시로 간주하고 해제.

    의도적으로 단일 슬롯: 게이트가 동작하는 한 미해결 카드는 항상 최대 1개다.
    """

    def __init__(
        self,
        *,
        resolve_timeout_sec: float = 90.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._now: Callable[[], float] = now or time.time
        self.resolve_timeout_sec = max(float(resolve_timeout_sec), 0.0)
        self._card: dict[str, Any] | None = None

    def mark_shown(self, intervention: dict[str, Any], answer: str = "") -> None:
        if not isinstance(intervention, dict):
            return
        event_id = str(intervention.get("event_id") or "").strip()
        if not event_id:
            return
        legacy_id = str(intervention.get("legacy_event_id") or "").strip()
        ids = {event_id} | ({legacy_id} if legacy_id else set())
        app_context = intervention.get("app_context")
        if not isinstance(app_context, dict):
            app_context = {}
        activity = intervention.get("activity_context")
        if not isinstance(activity, dict):
            activity = {}
        answer = str(answer or "")
        with self._lock:
            current = self._card
            if current is not None and ids & current["ids"]:
                # 같은 카드의 스트리밍 갱신 — shown_at은 첫 표시 시점 유지하되,
                # 최신 답변 텍스트는 갱신(retry의 avoid_text로 쓰임).
                current["ids"] |= ids
                if answer:
                    current["answer"] = answer
                return
            self._card = {
                "ids": ids,
                "document_key": str(
                    app_context.get("document_key")
                    or activity.get("document_key")
                    or ""
                ),
                "paragraph_fingerprint": str(
                    activity.get("paragraph_fingerprint") or ""
                ),
                "intervention_type": str(
                    intervention.get("intervention_type") or ""
                ),
                # 직전 제안 텍스트 — "다시"(retry) 시 avoid_text로 넘겨 새 문장이
                # 같은 걸 반복하지 않게 한다.
                "answer": answer,
                "shown_at": float(self._now()),
            }

    def resolve(self, event_id: str) -> dict[str, Any] | None:
        """event_id(또는 alias)가 현재 슬롯과 일치하면 해제하고 카드 정보 반환."""
        event_id = str(event_id or "").strip()
        if not event_id:
            return None
        with self._lock:
            card = self._card
            if card is None or event_id not in card["ids"]:
                return None
            self._card = None
            return dict(card)

    def poll(self) -> tuple[bool, dict[str, Any] | None]:
        """(게이트 활성?, 방금 만료된 카드). 만료된 카드는 호출자가 '무시(ignore)'
        신호로 페이싱에 반영할 수 있도록 한 번만 반환된다."""
        with self._lock:
            card = self._card
            if card is None:
                return False, None
            if self.resolve_timeout_sec > 0 and (
                self._now() - card["shown_at"] >= self.resolve_timeout_sec
            ):
                # 무반응 만료 — 사용자가 카드를 무시했다고 보고 게이트를 푼다.
                self._card = None
                return False, dict(card)
            return True, None

    def active(self) -> bool:
        return self.poll()[0]

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            card = self._card
            if card is None:
                return None
            return {
                "event_ids": sorted(card["ids"]),
                "document_key": card["document_key"],
                "intervention_type": card["intervention_type"],
                "shown_at": card["shown_at"],
                "age_sec": round(self._now() - card["shown_at"], 1),
            }
class ScreenContextService:
    """OCR/PID를 주기적으로 수집하고 agent용 최종 context를 저장합니다."""
    # consumer 중단으로 인해 큐에 끼인 항목을 점유로 간주하지 않기 위해 LLM timeout 시간보다 큰 경우에만 큐에 남은 항목을 인플라이트로 간주하도록 함
    INTERVENTION_MAX_INFLIGHT_SEC = 300.0

    # 수집기/필터/detector/scheduler/dispatcher 구성, 공유 시나리오 리스트 주입
    def __init__(
        self,
        root: str | Path,
        *,
        interval_sec: float = 3.0,
        ocr_language: str = "ko-KR",
        ocr_scale: float = 2.0,
        crop_left: int = 0,
        crop_top: int = 0,
        crop_right: int = 0,
        crop_bottom: int = 0,
        console_log: bool = False,
        llm=None,
        custom_document_tools: list[dict[str, str]] | None = None,
    ) -> None:
        self.interval_sec = interval_sec
        self.console_log = console_log
        self.llm = llm
        self.window_reader = WindowContextReader()
        self.screen_capture = ScreenCapture(
            crop_left=crop_left,
            crop_top=crop_top,
            crop_right=crop_right,
            crop_bottom=crop_bottom,
        )
        self.ocr_engine = OcrEngine(language=ocr_language, scale=ocr_scale)
        self.powerpoint_reader = PowerPointComReader()
        self.ui_reader = UiAutomationReader()
        self.content_filter = ContentFilter(custom_document_tools=custom_document_tools)
        # console_log(=--screen-debug)면 capture log를 debug/에 기록
        self.store = ScreenContextStore(root, debug=console_log)

        # Build one shared scenario list and inject it into every component
        # that depends on it (detector, scheduler, dispatcher). This replaces
        # the previous sequence where the detector's default scenarios were
        # used to seed the scheduler and the scheduler was then attached back
        # to the detector via a post-construction setter — which would silently
        # drift if anyone replaced detector.scenarios at runtime.
        # 등록 시나리오 = **커서-로컬 작성 도움** 3종만. native editor와 동일한
        # 모델: "지금 커서에서 글쓰기를 돕는다". 문서 전역 리뷰 시나리오(acronym/
        # citation/quote/heading/outline/list/code/todo/question/repeat/transition/
        # modifier/edit-diff/whole-doc-review/long-static-review)는 커서와 무관한
        # 제안을 만들어 위치 불일치·OCR 쓰레기·시나리오 폭주의 근원이었으므로
        # 외부 surface에서 제거. (클래스는 import 가능하게 남아있음 — 추후 명시적
        # "문서 검토" 버튼/명령으로 재도입 가능.) idle=이어쓰기, churn=막힌 문단
        # 재작성, blank=빈 문서 시작 — 셋 다 커서 상태만 보고 다른 곳을 안 가리킨다.
        scenarios = [
            IdleAfterWritingScenario(),
            ParagraphChurnScenario(),
            BlankDocumentStartScenario(),
        ]
        # 발화 페이스 (env로 운영 튜닝 가능) — 고정 간격이 아니라 적응형:
        # 발화 허용 = elapsed ≥ floor AND (elapsed ≥ base×multiplier OR 새 내용).
        # multiplier는 카드 반응(수락↓/거절·무시↑)으로 변하고 반감기 감쇠로 1.0에
        # 수렴 — 반응이 좋고 새 글을 쓰는 사용자는 floor(20초) 페이스까지,
        # 무시/거절이 쌓이면 ceil(4분)까지 물러난다.
        # paragraph_cooldown: 같은 단락(fingerprint)에는 시나리오가 달라도
        # 일정 시간 재발화 금지 — CFS 공정성이 같은 단락에서 매번 다른
        # 시나리오를 뽑아 "다양한 스팸"이 되는 구멍을 막는다.
        self.scenario_scheduler = ScenarioScheduler(
            self.store,
            scenarios=scenarios,
            fire_interval_floor_sec=_env_float("VERITAS_SCREEN_FIRE_FLOOR_S", 20.0),
            fire_interval_base_sec=_env_float("VERITAS_SCREEN_FIRE_BASE_S", 30.0),
            fire_interval_ceil_sec=_env_float("VERITAS_SCREEN_FIRE_CEIL_S", 240.0),
            pace_decay_half_life_sec=_env_float(
                "VERITAS_SCREEN_FIRE_DECAY_HALFLIFE_S", 600.0
            ),
            early_release_min_new_chars=int(
                _env_float("VERITAS_SCREEN_EARLY_RELEASE_CHARS", 80)
            ),
            paragraph_cooldown_sec=_env_float(
                "VERITAS_SCREEN_PARAGRAPH_COOLDOWN_S", 180.0
            ),
            console_log=console_log,
        )
        # 표시된 카드에 사용자가 반응할 때까지(또는 만료까지) 새 발화를 막는 게이트.
        self.unresolved_card_gate = UnresolvedCardGate(
            resolve_timeout_sec=_env_float(
                "VERITAS_SCREEN_CARD_RESOLVE_TIMEOUT_S", 90.0
            ),
        )
        # native-style caret-continuation 엔진 — 발화 결정의 실소유자.
        # cursor_scope가 N폴 안정 + 커서 확정이면 즉시 이어쓰기 발화. 시나리오/CFS/
        # idle-gate를 대체해 네이티브 ghostwrite 속도/동작을 외부 앱에 가져온다.
        self.continuation_engine = CaretContinuationEngine(
            stable_polls=int(_env_float("VERITAS_SCREEN_STABLE_POLLS", 2)),
            min_prefix_chars=int(_env_float("VERITAS_SCREEN_MIN_PREFIX_CHARS", 20)),
        )
        # 진입 직후 발화 보류 시간(초). 첫 캡처의 caret이 사용자가 쓰려는 곳이
        # 아닐 수 있어, 커서가 자리잡을 여유를 준다.
        self._start_grace_sec = _env_float("VERITAS_SCREEN_START_GRACE_S", 1.5)
        self._monitor_started_at = 0.0
        # LLM-backed selection (replaces CFS vruntime ranking when enabled). Built
        # only when an llm is available; the detector falls back to CFS otherwise.
        self.scenario_router = ScenarioRouter(llm) if llm is not None else None
        self.intervention_detector = InterventionDetector(
            scenarios=scenarios,
            scheduler=self.scenario_scheduler,
            router=self.scenario_router,
        )
        self.intervention_dispatcher = InterventionDispatcher(
            self.store,
            scenarios={s.name: s for s in scenarios},
            console_log=console_log,
        )

        self._previous_active_text = ""
        self._last_poll_error: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._debug_stats = self._new_debug_stats()

    # 한 번 캡처: 텍스트 수집 -> filtered -> decide -> 이벤트 저장 -> dispatch
    def capture_once(self) -> ScreenContextEvent:
        window = self.window_reader.read_foreground()
        app_text = self._read_app_text_first(window)
        ui_automation = (
            UiAutomationResult(error="skipped: app text extraction succeeded.")
            if self._is_usable_app_text(app_text)
            else self._read_text_first(window)
        )
        if self._is_usable_app_text(app_text):
            ocr = OcrResult(
                language=self.ocr_engine.language,
                error="skipped: app text extraction succeeded.",
            )
        elif self._is_usable_text_source(ui_automation):
            ocr = OcrResult(
                language=self.ocr_engine.language,
                error="skipped: UI Automation text extraction succeeded.",
            )
        elif ui_automation.reject_reason == "empty_text":
            # UIA found and read the focused editor control but it is genuinely
            # empty (reject_reason="empty_text"). Do NOT fall back to OCR: OCR
            # would just read the app's menus/toolbars as noise. Treat it as an
            # empty document so BlankDocumentStartScenario handles it instead.
            ocr = OcrResult(
                language=self.ocr_engine.language,
                error="skipped: UIA read an empty editor (authoritative; no OCR fallback).",
            )
        else:
            image = self.screen_capture.capture_window(window)
            ocr = self.ocr_engine.recognize(image)

        filtered = self.content_filter.build(
            window=window,
            app_text=app_text,
            ocr=ocr,
            ui_automation=ui_automation,
            previous_text=self._previous_active_text,
        )
        history_events = self.store.load_recent(self.intervention_detector.history_window - 1)
        # 두 점유 게이트: (1) 큐/LLM 생성 중(pipeline_busy), (2) 표시된 카드가
        # 아직 미해결(unresolved card). 어느 쪽이든 새 개입을 스케줄하지 않는다.
        pipeline_busy = self._intervention_pipeline_busy()
        card_unresolved, expired_card = self.unresolved_card_gate.poll()
        # native-style caret-continuation 엔진이 발화 결정 소유 (시나리오/dwell/CFS
        # 대체). cursor_scope가 N폴 안정 + 커서 확정이면 즉시 이어쓰기 발화.
        document_key = self.intervention_detector._make_document_key(window)
        in_grace = (
            self._monitor_started_at > 0
            and self._start_grace_sec > 0
            and (time.monotonic() - self._monitor_started_at) < self._start_grace_sec
        )
        fire = self.continuation_engine.observe(
            document_key=document_key,
            filtered=filtered,
            busy=pipeline_busy,
            card_active=card_unresolved,
            suppressed=in_grace,
        )
        if fire.fire and fire.intervention is not None:
            intervention = fire.intervention
            # dispatcher의 activity_context / 프론트 카드 교체 / 카드 게이트 doc
            # 추적이 쓰는 식별자를 채운다.
            intervention.metadata.setdefault("document_key", document_key)
            intervention.metadata.setdefault(
                "paragraph_fingerprint",
                self.intervention_detector._fingerprint(filtered.cursor_scope_text or ""),
            )
        else:
            intervention = InterventionDecision(
                should_consider_llm=False,
                intervention_type="none",
                reason_codes=[fire.reason] if fire.reason else [],
                metadata={"document_key": document_key, "engine": "caret_continuation"},
            )
        diagnostics = self.diagnose_capture(
            window=window,
            app_text=app_text,
            ui_automation=ui_automation,
            ocr=ocr,
            filtered=filtered,
            intervention=intervention,
        )

        self._previous_active_text = filtered.active_editor_text
        event = ScreenContextEvent.new(
            event_id=self._new_event_id(),
            window=window,
            ocr=ocr,
            app_text=app_text,
            ui_automation=ui_automation,
            filtered=filtered,
            intervention=intervention,
            diagnostics=diagnostics,
        )
        self.store.save_event(event)
        self.intervention_dispatcher.dispatch(event)
        self._log_capture_event(event)
        return event

    # ----------------------------------------------------- unresolved card gate

    def mark_card_shown(self, intervention: dict, answer: str = "") -> None:
        """카드가 실제로 렌더되기 시작했음을 기록 (ChatAgent 답변 콜백 경유).
        ``answer``는 직전 제안 텍스트 — retry의 avoid_text로 쓰인다."""
        self.unresolved_card_gate.mark_shown(intervention, answer=answer)

    def resolve_card(self, event_id: str, *, feedback_action: str = "") -> bool:
        """사용자 feedback으로 카드를 해결 처리. '다시'(retry)면 caret-continuation
        엔진에 **즉시 재발화**를 예약하고 직전 제안을 avoid_text로 넘긴다 — 다음
        폴링(≤1초)에서 같은 자리에 다른 문장이 나온다(idle-gate를 안 거침)."""
        card = self.unresolved_card_gate.resolve(event_id)
        if card is None:
            return False
        action = str(feedback_action or "").strip().lower()
        document_key = str(card.get("document_key") or "")
        if document_key and action == "retry":
            self.continuation_engine.request_retry(
                document_key,
                avoid_text=str(card.get("answer") or ""),
                # 원래 카드 id를 재사용해 재발화가 새 카드 대신 같은 카드를 갱신.
                target_event_id=str(event_id or ""),
            )
        return True

    # 큐에 in-flight 개입이 있으면 True. consumer가 LLM 처리 동안 큐에서 제거하지 않으므로 큐 non-empty = 파이프라인 점유
    # consumer 중단으로 끼인 항목(captured_at age > INTERVENTION_MAX_INFLIGHT_SEC)은 제외
    def _intervention_pipeline_busy(self) -> bool:
        pending = self.store.load_pending_interventions(limit=1)
        if not pending:
            return False
        captured_at = str(pending[0].get("captured_at") or "").strip()
        if not captured_at:
            return True
        try:
            age = (datetime.now() - datetime.fromisoformat(captured_at)).total_seconds()
        except ValueError:
            return True
        return age <= self.INTERVENTION_MAX_INFLIGHT_SEC

    # 저장된 이벤트로부터 진단 dict를 재계산
    def diagnose_event(self, event: ScreenContextEvent) -> dict:
        return self.diagnose_capture(
            window=event.window,
            app_text=event.app_text,
            ui_automation=event.ui_automation,
            ocr=event.ocr,
            filtered=event.filtered,
            intervention=event.intervention,
        )

    # 캡처 구성요소로 진단 dict 생성 (텍스트 유무/소스/confidence/개입 여부)
    def diagnose_capture(
        self,
        *,
        window,
        app_text: AppTextResult,
        ui_automation: UiAutomationResult,
        ocr: OcrResult,
        filtered,
        intervention,
    ) -> dict:
        text = (filtered.active_editor_text or "").strip()
        current_paragraph = (filtered.current_paragraph_text or "").strip()
        text_source = self._diagnose_text_source(
            app_text=app_text,
            ui_automation=ui_automation,
            ocr=ocr,
            filtered=filtered,
        )
        has_foreground_window = bool(window.hwnd and not window.error)
        has_text = bool(text)
        has_current_paragraph = bool(current_paragraph)
        usable_for_llm = bool(has_foreground_window and has_text and filtered.confidence > 0)

        return {
            "has_foreground_window": has_foreground_window,
            "has_text": has_text,
            "has_current_paragraph": has_current_paragraph,
            "text_source": text_source,
            "confidence": filtered.confidence,
            "active_text_chars": len(text),
            "current_paragraph_chars": len(current_paragraph),
            "browser_url": ui_automation.browser_url,
            "usable_for_llm": usable_for_llm,
            "intervention_queued": bool(intervention.should_consider_llm),
            "intervention_blockers": (
                (intervention.metadata or {}).get("blockers")
                if not intervention.should_consider_llm
                else []
            ),
            "errors": {
                "window": window.error,
                "app_text": app_text.error,
                "ui_automation": ui_automation.error,
                "ocr": ocr.error,
            },
        }

    # active_editor_text가 어느 소스(app_text/UIA/OCR)에서 왔는지 판별
    def _diagnose_text_source(
        self,
        *,
        app_text: AppTextResult,
        ui_automation: UiAutomationResult,
        ocr: OcrResult,
        filtered,
    ) -> str:
        active_text = (filtered.active_editor_text or "").strip()
        if not active_text:
            return "none"
        if (app_text.text or "").strip() == active_text:
            return app_text.text_source or "app_text"
        if (ui_automation.text or "").strip() == active_text:
            return ui_automation.text_source or "ui_automation"
        if (ocr.text or "").strip() == active_text:
            return "ocr"
        return "filtered"

    # scenario_scheduler 시작 + 폴링 스레드 기동
    def start_polling(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._debug_stats = self._new_debug_stats()
        self._debug_stats["started_at"] = datetime.now().isoformat(timespec="seconds")
        # 진입 직후 startup grace 기준점 — 첫 1~2초는 발화 보류(커서 자리잡을 시간).
        # 방금 창을 전환해 들어온 첫 캡처의 caret이 엉뚱한 위치일 수 있어서.
        self._monitor_started_at = time.monotonic()
        # 엔진 상태 초기화 — 이전 세션의 안정/dedup이 남아 즉시 발화하지 않게.
        self.continuation_engine.reset()
        self.scenario_scheduler.start()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    # 폴링 스레드 정지 + scenario_scheduler 정지(상태 flush)
    def stop_polling(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.interval_sec + 1)
        self.scenario_scheduler.stop()
        # debug 모드면 세션 통계 1줄을 capture log 끝에 append
        if self.console_log:
            self._write_debug_stats_summary()

    # 폴링 스레드가 살아있는지 여부
    def is_polling(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # 마지막 폴링 사이클의 에러 메시지 (없으면 None)
    def last_poll_error(self) -> str | None:
        return self._last_poll_error

    # interval_sec 주기로 capture_once를 반복 실행하는 폴링 루프
    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            started_at = time.monotonic()
            try:
                self.capture_once()
                self._last_poll_error = None
            except Exception as exc:
                self._last_poll_error = f"{type(exc).__name__}: {exc}"
                if self.console_log:
                    print(f"[screen_context][poll][error] {self._last_poll_error}")
            elapsed = time.monotonic() - started_at
            remaining = max(self.interval_sec - elapsed, 0.0)
            self._stop_event.wait(remaining)

    # 캡처 이벤트를 capture log에 기록, console_log면 콘솔에도 출력
    def _log_capture_event(self, event: ScreenContextEvent) -> None:
        diagnostics = event.diagnostics or {}
        blockers = diagnostics.get("intervention_blockers") or []
        errors = diagnostics.get("errors") or {}
        if not isinstance(errors, dict):
            errors = {}

        window = event.window
        title = " ".join((window.window_title or "").split())
        if len(title) > 120:
            title = title[:117] + "..."

        log_payload = {
            "type": "screen_capture",
            "event_id": event.event_id,
            "captured_at": event.captured_at,
            "window": {
                "process_name": window.process_name,
                "window_title": window.window_title,
                "pid": window.pid,
                "hwnd": window.hwnd,
            },
            "browser_url": event.ui_automation.browser_url,
            "diagnostics": diagnostics,
            "filtered": {
                "active_app_type": event.filtered.active_app_type,
                "active_text_chars": len((event.filtered.active_editor_text or "").strip()),
                "current_paragraph_chars": len((event.filtered.current_paragraph_text or "").strip()),
                "changed_text_chars": len((event.filtered.changed_text or "").strip()),
                "current_paragraph_source": event.filtered.current_paragraph_source,
            },
            "intervention": {
                "should_consider_llm": event.intervention.should_consider_llm,
                "intervention_type": event.intervention.intervention_type,
                "score": event.intervention.score,
                "priority": event.intervention.priority,
                "reason_codes": event.intervention.reason_codes,
                "metadata": event.intervention.metadata,
            },
        }
        self.store.append_capture_log(log_payload)

        if not self.console_log:
            return

        self._accumulate_debug_stats(event)

        base = (
            f"[screen_context][capture] event={event.event_id} "
            f"process={window.process_name or '-'} "
            f"title={title!r} "
            f"source={diagnostics.get('text_source', 'unknown')} "
            f"chars={diagnostics.get('active_text_chars', 0)} "
            f"confidence={diagnostics.get('confidence', 0.0)} "
            f"queued={diagnostics.get('intervention_queued', False)} "
            f"type={event.intervention.intervention_type}"
        )
        if blockers:
            base += f" blockers={blockers}"
        print(base)
        if diagnostics.get("has_text"):
            self._log_debug_text(event, diagnostics)
        self._log_debug_decision(event)

        if not diagnostics.get("has_foreground_window"):
            print(
                "[screen_context][capture][warn] "
                f"event={event.event_id} window_error={errors.get('window') or 'unknown'}"
            )
        elif not diagnostics.get("has_text"):
            extractor_errors = {
                key: value
                for key, value in errors.items()
                if key in {"app_text", "ui_automation", "ocr"} and value
            }
            print(
                "[screen_context][capture][warn] "
                f"event={event.event_id} no_readable_text errors={extractor_errors}"
            )

    # debug 모드 세션 통계 누적기 초기 구조
    def _new_debug_stats(self) -> dict:
        return {
            "started_at": None,
            "total_captures": 0,
            "common_passed": 0,
            "common_blocked": 0,
            "block_reasons": {},
            "scenarios": {},
            "interventions_queued": 0,
        }

    # 캡처 1건의 결정 결과를 debug 세션 통계에 누적
    def _accumulate_debug_stats(self, event: ScreenContextEvent) -> None:
        stats = self._debug_stats
        stats["total_captures"] += 1
        meta = event.intervention.metadata or {}
        common_checks = meta.get("common_checks") or {}
        blockers = [
            name
            for name, check in common_checks.items()
            if isinstance(check, dict) and not check.get("passed")
        ]
        if blockers:
            stats["common_blocked"] += 1
            for name in blockers:
                stats["block_reasons"][name] = stats["block_reasons"].get(name, 0) + 1
        else:
            stats["common_passed"] += 1
        for name, scenario in (meta.get("scenarios") or {}).items():
            if not isinstance(scenario, dict):
                continue
            entry = stats["scenarios"].setdefault(name, {"ready": 0, "selected": 0})
            if scenario.get("ready"):
                entry["ready"] += 1
        selected = meta.get("selected")
        if selected:
            entry = stats["scenarios"].setdefault(selected, {"ready": 0, "selected": 0})
            entry["selected"] += 1
        if event.intervention.should_consider_llm:
            stats["interventions_queued"] += 1

    # debug 모드 종료 시 세션 통계 1줄을 capture log 끝에 append
    def _write_debug_stats_summary(self) -> None:
        stats = self._debug_stats
        summary = {
            "type": "session_stats",
            "session_id": self.store.capture_session_id,
            "started_at": stats.get("started_at"),
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "total_captures": stats["total_captures"],
            "common_gate": {
                "passed": stats["common_passed"],
                "blocked": stats["common_blocked"],
                "block_reasons": stats["block_reasons"],
            },
            "scenarios": stats["scenarios"],
            "interventions_queued": stats["interventions_queued"],
        }
        self.store.append_capture_log(summary)

    # 디버그용: 추출 텍스트 길이·미리보기를 콘솔 출력
    def _log_debug_text(self, event: ScreenContextEvent, diagnostics: dict) -> None:
        filtered = event.filtered
        print(
            "[screen_context][text] "
            f"source={diagnostics.get('text_source', 'unknown')} "
            f"active_chars={len((filtered.active_editor_text or '').strip())} "
            f"paragraph_chars={len((filtered.current_paragraph_text or '').strip())} "
            f"changed_chars={len((filtered.changed_text or '').strip())} "
            f"preview={self._preview_text(filtered.current_paragraph_text or filtered.active_editor_text)!r}"
        )
        if (event.ocr.text or "").strip():
            print(
                "[screen_context][ocr] "
                f"chars={len((event.ocr.text or '').strip())} "
                f"lines={len(event.ocr.lines or [])} "
                f"preview={self._preview_text(event.ocr.text)!r}"
            )

    # 디버그용: 공통 게이트/시나리오 게이트/선택 결과를 콘솔 출력
    def _log_debug_decision(self, event: ScreenContextEvent) -> None:
        metadata = event.intervention.metadata or {}
        common_checks = metadata.get("common_checks") or {}
        if isinstance(common_checks, dict):
            for name in ("editing_app", "dwell", "stable_paragraph"):
                check = common_checks.get(name) or {}
                if not isinstance(check, dict):
                    continue
                status = "PASS" if check.get("passed") else "BLOCK"
                detail = self._format_common_check_detail(name, check)
                print(f"[screen_context][decision] common.{name}={status} {detail}".rstrip())

        scenarios = metadata.get("scenarios") or {}
        if isinstance(scenarios, dict):
            for scenario_name, scenario in scenarios.items():
                if not isinstance(scenario, dict):
                    continue
                status = "READY" if scenario.get("ready") else "WAIT"
                gates = scenario.get("gate_results") or {}
                gate_summary = []
                if isinstance(gates, dict):
                    for gate_name, gate in gates.items():
                        if not isinstance(gate, dict):
                            continue
                        gate_status = "P" if gate.get("passed") else "B"
                        gate_summary.append(f"{gate_name}={gate_status}")
                gate_text = " ".join(gate_summary)
                print(
                    f"[screen_context][decision] scenario.{scenario_name}={status} "
                    f"score={scenario.get('score', 0.0)} {gate_text}".rstrip()
                )

        selected = metadata.get("selected")
        if selected:
            scheduler = metadata.get("scheduler") or {}
            vruntimes = scheduler.get("vruntimes") if isinstance(scheduler, dict) else None
            print(
                f"[screen_context][decision] selected={selected} "
                f"vruntimes={vruntimes}"
            )

    # 공통 게이트별 디버그 출력 문자열 포맷
    def _format_common_check_detail(self, name: str, check: dict) -> str:
        reason = str(check.get("reason") or "")
        if name == "editing_app":
            return f"reason={reason} app_type={check.get('active_app_type') or '-'}"
        if name == "dwell":
            return (
                f"reason={reason} "
                f"history={check.get('history_count')}/{check.get('min_history_count')} "
                f"dwell={check.get('dwell_ratio')}/{check.get('dwell_threshold')}"
            )
        if name == "stable_paragraph":
            return (
                f"reason={reason} "
                f"source={check.get('current_paragraph_source') or '-'} "
                f"chars={check.get('current_paragraph_chars')} "
                f"min={check.get('min_paragraph_chars')} "
                f"ocr_min={check.get('min_ocr_paragraph_chars')} "
                f"confidence={check.get('confidence')}"
            )
        return f"reason={reason}"

    # 텍스트를 공백 정규화 후 limit 길이로 잘라 미리보기 문자열 생성
    def _preview_text(self, text: str, *, limit: int = 220) -> str:
        value = " ".join(str(text or "").split()).strip()
        if len(value) <= limit:
            return value
        return value[: limit - 3] + "..."

    # 현재 시각 기반 이벤트 ID 생성 (YYYYMMDD_HHMMSS_us)
    def _new_event_id(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    # 텍스트 추출 대상 앱이면 UI Automation으로 focused 텍스트를 읽음
    def _read_text_first(self, window) -> UiAutomationResult:
        if not self._is_text_extraction_target(window):
            return UiAutomationResult(error="skipped: foreground app is not a text extraction target.")
        return self.ui_reader.read_focused(window)

    # foreground 앱이 텍스트 추출 대상인지(프로세스명·확장자) 판별
    def _is_text_extraction_target(self, window) -> bool:
        return is_text_extraction_target(window)

    # UI Automation 결과가 쓸 만한지(텍스트 존재 + 품질) 판별
    def _is_usable_text_source(self, result: UiAutomationResult) -> bool:
        return bool(result.text and result.source_quality in {"primary", "usable"})

    # 앱 전용 reader로 텍스트를 읽음 (현재 PowerPoint COM만)
    def _read_app_text_first(self, window) -> AppTextResult:
        if (window.process_name or "").lower() == "powerpnt.exe":
            return self.powerpoint_reader.read_active_slide(window)
        return AppTextResult(error="skipped: no app-specific text reader.")

    # 앱 전용 추출 결과가 쓸 만한지(텍스트 존재 + 품질) 판별
    def _is_usable_app_text(self, result: AppTextResult) -> bool:
        return bool(result.text and result.source_quality in {"primary", "usable"})
