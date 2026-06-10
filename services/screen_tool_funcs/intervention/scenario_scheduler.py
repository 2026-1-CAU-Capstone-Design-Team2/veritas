from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.store import ScreenContextStore
from ..scenario import ScenarioType


@dataclass
class ScenarioWeights:
    """Per-scenario CFS weights resolved from each ScenarioType."""

    initial_vruntime: float
    vruntime_increment: float


@dataclass
class ScenarioSchedulerState:
    """In-memory CFS state for one document_key."""

    document_key: str
    initial_vruntimes: dict[str, float] = field(default_factory=dict)
    vruntimes: dict[str, float] = field(default_factory=dict)
    last_decay_at: float = 0.0
    last_activity_at: float = 0.0
    last_reset_at: float = 0.0
    # {시나리오명: unix_ts} — 각 시나리오가 마지막으로 선택(발동)된 캡처 시각.
    # 시간 기반 cooldown 게이트가 읽음.
    last_fired_at: dict[str, float] = field(default_factory=dict)
    # {시나리오명: 발동 시점의 정규화 문서 길이}. last_fired_at과 짝을 이루는 보조 데이터.
    # "직전 리뷰 이후 추가된 글자 수" 같은 글자수 기반 cooldown 판정에 사용.
    last_fired_doc_chars: dict[str, int] = field(default_factory=dict)
    # 시나리오 무관 가장 최근 발동 시각. 스케줄러의 전역 rate-limit이 사용.
    last_global_fire_at: float = 0.0
    # {paragraph_fingerprint: unix_ts} — 그 단락에서 (시나리오 무관) 마지막으로 발동한 시각.
    # 시나리오별 cooldown은 같은 시나리오의 재발화만 막아서, 24개 시나리오가 같은 단락을
    # 돌아가며 발화하는 "서로 다른 내용의 카드 폭주"를 못 막는다. 이 맵이 그 cross-scenario
    # 구멍을 막는다. 단락을 실제로 수정하면 fingerprint가 바뀌어 자연히 풀린다.
    last_fired_paragraphs: dict[str, float] = field(default_factory=dict)
    # ---- 적응형 발화 페이스(문서당) ----
    # 발화 간격 = base × multiplier (clamp [floor, ceil]). multiplier는 카드 반응에
    # 따라 변하고(수락↓ / 거절·무시↑) 시간이 지나면 1.0으로 반감기 감쇠한다.
    fire_pace_multiplier: float = 1.0
    pace_updated_at: float = 0.0
    # 직전 발화 시점의 정규화 문서 길이 / 단락 fingerprint — "그 뒤로 새 내용을
    # 썼는가"(조기 해제) 판정 기준. -1 = 기준 없음(레거시 상태).
    last_global_fire_doc_chars: int = -1
    last_global_fire_paragraph_fp: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "document_key": self.document_key,
            "initial_vruntimes": dict(self.initial_vruntimes),
            "vruntimes": dict(self.vruntimes),
            "last_decay_at": self.last_decay_at,
            "last_activity_at": self.last_activity_at,
            "last_reset_at": self.last_reset_at,
            "last_fired_at": dict(self.last_fired_at),
            "last_fired_doc_chars": dict(self.last_fired_doc_chars),
            "last_global_fire_at": self.last_global_fire_at,
            "last_fired_paragraphs": dict(self.last_fired_paragraphs),
            "fire_pace_multiplier": self.fire_pace_multiplier,
            "pace_updated_at": self.pace_updated_at,
            "last_global_fire_doc_chars": self.last_global_fire_doc_chars,
            "last_global_fire_paragraph_fp": self.last_global_fire_paragraph_fp,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ScenarioSchedulerState":
        raw_fire_chars = payload.get("last_global_fire_doc_chars")
        return cls(
            document_key=str(payload.get("document_key") or ""),
            initial_vruntimes={
                str(k): float(v)
                for k, v in (payload.get("initial_vruntimes") or {}).items()
            },
            vruntimes={
                str(k): float(v)
                for k, v in (payload.get("vruntimes") or {}).items()
            },
            last_decay_at=float(payload.get("last_decay_at") or 0.0),
            last_activity_at=float(payload.get("last_activity_at") or 0.0),
            last_reset_at=float(payload.get("last_reset_at") or 0.0),
            last_fired_at={
                str(k): float(v)
                for k, v in (payload.get("last_fired_at") or {}).items()
            },
            last_fired_doc_chars={
                str(k): int(v)
                for k, v in (payload.get("last_fired_doc_chars") or {}).items()
            },
            last_global_fire_at=float(payload.get("last_global_fire_at") or 0.0),
            last_fired_paragraphs={
                str(k): float(v)
                for k, v in (payload.get("last_fired_paragraphs") or {}).items()
            },
            fire_pace_multiplier=float(payload.get("fire_pace_multiplier") or 1.0),
            pace_updated_at=float(payload.get("pace_updated_at") or 0.0),
            last_global_fire_doc_chars=(
                int(raw_fire_chars) if raw_fire_chars is not None else -1
            ),
            last_global_fire_paragraph_fp=str(
                payload.get("last_global_fire_paragraph_fp") or ""
            ),
        )


class ScenarioScheduler:
    """CFS-like scenario scheduler.

    - Per-document state, persisted as JSON via ScreenContextStore.
    - vruntime decay is applied lazily on each access (elapsed-time based).
    - A background flush thread writes loaded states to disk at flush_interval_sec.
    - Reset policy: state is reset to initial vruntimes when either
        (now - last_activity_at) >= reset_idle_sec  OR
        (now - last_reset_at)    >= reset_interval_sec.
    """

    def __init__(
        self,
        store: ScreenContextStore,
        *,
        scenarios: list[ScenarioType] | None = None,
        weights: dict[str, ScenarioWeights] | None = None,
        decay_per_second: float = 0.05,
        flush_interval_sec: float = 600.0,
        reset_idle_sec: float = 3600.0,
        reset_interval_sec: float = 7200.0,
        max_documents: int = 50,
        fire_interval_floor_sec: float = 20.0,
        fire_interval_base_sec: float = 30.0,
        fire_interval_ceil_sec: float = 240.0,
        pace_decay_half_life_sec: float = 600.0,
        early_release_min_new_chars: int = 80,
        paragraph_cooldown_sec: float = 180.0,
        console_log: bool = False,
    ) -> None:
        if scenarios is not None and weights is not None:
            raise ValueError("Pass either scenarios= or weights=, not both.")
        if scenarios is None and weights is None:
            raise ValueError("ScenarioScheduler requires scenarios= (preferred) or weights=.")
        self.store = store
        if scenarios is not None:
            self.weights = {
                scenario.name: ScenarioWeights(
                    initial_vruntime=scenario.initial_vruntime,
                    vruntime_increment=scenario.vruntime_increment,
                )
                for scenario in scenarios
            }
        else:
            self.weights = dict(weights or {})
        self.decay_per_second = max(decay_per_second, 0.0)
        self.flush_interval_sec = max(flush_interval_sec, 1.0)
        self.reset_idle_sec = max(reset_idle_sec, 0.0)
        self.reset_interval_sec = max(reset_interval_sec, 0.0)
        self.max_documents = max(max_documents, 1)
        # ---- 적응형 발화 페이스 (시나리오 무관, 문서당) ----
        # 발화 허용 = elapsed ≥ floor AND (elapsed ≥ base×multiplier OR 새 내용).
        # floor: 어떤 경우에도 지켜지는 발화 간 최소 간격 (스팸 절대 하한).
        # base×multiplier: 평상시 간격. multiplier는 카드 반응(수락 0.6 / 다시 0.7 /
        #   거절 1.7 / 무시 1.3)으로 변하고 반감기 감쇠로 1.0에 수렴 — "도움이 됐으면
        #   더 자주, 방해였으면 더 드물게". clamp [floor, ceil].
        # 새 내용(조기 해제): 직전 발화 이후 정규화 문서 길이가
        #   early_release_min_new_chars 이상 변했으면 간격이 안 지났어도 발화 허용
        #   — 벽시계가 아니라 "도울 거리가 생겼는가"가 기준.
        self.fire_interval_floor_sec = max(fire_interval_floor_sec, 0.0)
        self.fire_interval_base_sec = max(fire_interval_base_sec, self.fire_interval_floor_sec)
        self.fire_interval_ceil_sec = max(fire_interval_ceil_sec, self.fire_interval_base_sec)
        self.pace_decay_half_life_sec = max(pace_decay_half_life_sec, 0.0)
        self.early_release_min_new_chars = max(int(early_release_min_new_chars), 0)
        # 같은 단락(fingerprint)에 시나리오 무관 재발화 금지 시간. 0 이하면 비활성.
        self.paragraph_cooldown_sec = max(paragraph_cooldown_sec, 0.0)
        self.console_log = console_log

        self._cache: dict[str, ScenarioSchedulerState] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._flush_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._flush_thread and self._flush_thread.is_alive():
            return
        self._stop_event.clear()
        # Prune any pre-existing files past the cap before the first flush.
        self._prune_disk_only()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=self.flush_interval_sec + 1)
        self._flush_thread = None
        self.flush_all()

    def get_state(self, document_key: str, *, now: float | None = None) -> ScenarioSchedulerState:
        """Return cached state for document_key after applying decay/reset."""
        now = now if now is not None else time.time()
        with self._lock:
            state = self._cache.get(document_key)
            if state is None:
                state = self._load_or_create(document_key, now=now)
                self._cache[document_key] = state
            self._maybe_reset(state, now=now)
            self._apply_decay(state, now=now)
            self._sync_known_scenarios(state)
            return state

    def select(
        self,
        document_key: str,
        ready_names: list[str],
        *,
        now: float | None = None,
    ) -> str | None:
        """Pick the scenario with the lowest vruntime among ready_names.

        Ties broken by initial_vruntime (rarer first), then by name for determinism.
        """
        if not ready_names:
            return None
        now = now if now is not None else time.time()
        state = self.get_state(document_key, now=now)
        scored: list[tuple[float, float, str]] = []
        for name in ready_names:
            if name not in self.weights:
                continue
            vruntime = state.vruntimes.get(name, self.weights[name].initial_vruntime)
            scored.append((vruntime, self.weights[name].initial_vruntime, name))
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        return scored[0][2]

    def charge(self, document_key: str, name: str, *, now: float | None = None) -> None:
        """Charge vruntime for the scenario that just won execution."""
        if name not in self.weights:
            return
        now = now if now is not None else time.time()
        with self._lock:
            state = self.get_state(document_key, now=now)
            current = state.vruntimes.get(name, self.weights[name].initial_vruntime)
            state.vruntimes[name] = current + self.weights[name].vruntime_increment
            state.last_activity_at = now

    def select_and_charge(
        self,
        document_key: str,
        ready_names: list[str],
        *,
        now: float | None = None,
        doc_chars: int | None = None,
        paragraph_fingerprint: str | None = None,
        trace_out: dict[str, Any] | None = None,
    ) -> str | None:
        """Pick the winner and charge its vruntime in a single decay step.

        Replaces a `select(...)` followed by `charge(...)`: each of those calls
        passes through `get_state()`, which applies lazy decay independently.
        Doing both inside one critical section with a single `now` guarantees
        decay is applied at most once per capture and that no concurrent
        `flush_loop`/`get_state` can mutate state between the two operations.

        trace_out: 호출자가 dict를 전달하면 결정 과정(후보·vruntime·선택·throttle·거부 사유)을
        그 dict에 채움. 디버깅·진단용. None이면 무시.
        """
        # trace는 None이어도 dict처럼 쓸 수 있게 로컬 buffer 사용
        trace: dict[str, Any] = trace_out if trace_out is not None else {}
        trace["ready_candidates"] = list(ready_names)
        trace["vruntimes_before"] = {}
        trace["selected"] = None
        trace["selected_vruntime_before"] = None
        trace["selected_vruntime_after"] = None
        trace["global_throttle"] = {
            "active": False,
            "reason": None,
            "elapsed_since_last_fire_sec": None,
            "floor_sec": self.fire_interval_floor_sec,
            "effective_interval_sec": None,
            "pace_multiplier": None,
            "early_release": False,
            "last_global_fire_at": 0.0,
        }
        trace["paragraph_throttle"] = {
            "active": False,
            "fingerprint": paragraph_fingerprint or None,
            "cooldown_sec": self.paragraph_cooldown_sec,
        }
        trace["rejected_reason"] = None

        if not ready_names:
            trace["rejected_reason"] = "no_ready_candidates"
            return None
        now = now if now is not None else time.time()
        with self._lock:
            state = self.get_state(document_key, now=now)
            trace["global_throttle"]["last_global_fire_at"] = state.last_global_fire_at
            # 시나리오 무관 적응형 발화 게이트 — floor / 적응 간격 / 새 내용 조기 해제.
            gate_reason = self._global_gate_locked(
                state,
                now=now,
                doc_chars=doc_chars,
                trace=trace["global_throttle"],
            )
            if gate_reason is not None:
                trace["global_throttle"]["active"] = True
                trace["global_throttle"]["reason"] = gate_reason
                trace["rejected_reason"] = gate_reason
                if self.console_log:
                    print(
                        "[screen_context][scheduler] "
                        f"fire gate ({gate_reason}): "
                        f"elapsed={trace['global_throttle']['elapsed_since_last_fire_sec']}s "
                        f"interval={trace['global_throttle']['effective_interval_sec']}s "
                        f"ready_names={ready_names}"
                    )
                return None
            # 단락 단위 cross-scenario cooldown — 같은 단락에는 시나리오가 달라도
            # paragraph_cooldown_sec 동안 추가 발화 금지.
            if self._paragraph_throttled_locked(
                state, paragraph_fingerprint, now=now
            ):
                trace["paragraph_throttle"]["active"] = True
                trace["rejected_reason"] = "paragraph_cooldown"
                if self.console_log:
                    print(
                        "[screen_context][scheduler] "
                        f"paragraph cooldown: fp={paragraph_fingerprint} "
                        f"ready_names={ready_names}"
                    )
                return None
            scored: list[tuple[float, float, str]] = []
            for name in ready_names:
                if name not in self.weights:
                    continue
                vruntime = state.vruntimes.get(name, self.weights[name].initial_vruntime)
                scored.append((vruntime, self.weights[name].initial_vruntime, name))
                trace["vruntimes_before"][name] = round(vruntime, 4)
            if not scored:
                trace["rejected_reason"] = "no_weights_match"
                return None
            scored.sort(key=lambda item: (item[0], item[1], item[2]))
            selected = scored[0][2]
            current = state.vruntimes.get(selected, self.weights[selected].initial_vruntime)
            new_vruntime = current + self.weights[selected].vruntime_increment
            state.vruntimes[selected] = new_vruntime
            state.last_activity_at = now
            # 당첨 시나리오의 마지막 발동 시각 기록
            state.last_fired_at[selected] = now
            # 전역 throttle 기준점 갱신
            state.last_global_fire_at = now
            if doc_chars is not None:
                # 발동 시점의 문서 길이 기록
                state.last_fired_doc_chars[selected] = doc_chars
            self._record_paragraph_fire_locked(state, paragraph_fingerprint, now=now)
            self._record_global_fire_baseline_locked(
                state, doc_chars=doc_chars, paragraph_fingerprint=paragraph_fingerprint
            )
            trace["selected"] = selected
            trace["selected_vruntime_before"] = round(current, 4)
            trace["selected_vruntime_after"] = round(new_vruntime, 4)
            return selected

    # 카드 반응 → 페이스 multiplier 계수. 수락/다시 = 더 자주, 거절/무시 = 더 드물게.
    PACE_FEEDBACK_FACTORS: dict[str, float] = {
        "accept": 0.6,
        "retry": 0.7,
        "reject": 1.7,
        "ignore": 1.3,
    }
    _PACE_MULTIPLIER_MIN = 0.5

    def global_gate_reason(
        self,
        document_key: str,
        *,
        doc_chars: int | None = None,
        now: float | None = None,
    ) -> str | None:
        """적응형 전역 발화 게이트의 router-path 진입점. 허용이면 None, 차단이면
        reason 코드(``global_throttle``=floor 미달 / ``adaptive_interval``=적응
        간격 미달 + 새 내용 없음)."""
        now = now if now is not None else time.time()
        with self._lock:
            state = self.get_state(document_key, now=now)
            return self._global_gate_locked(state, now=now, doc_chars=doc_chars)

    def _global_gate_locked(
        self,
        state: ScenarioSchedulerState,
        *,
        now: float,
        doc_chars: int | None,
        trace: dict[str, Any] | None = None,
    ) -> str | None:
        if state.last_global_fire_at <= 0:
            return None
        elapsed = now - state.last_global_fire_at
        interval, multiplier = self._effective_interval_locked(state, now=now)
        if trace is not None:
            trace["elapsed_since_last_fire_sec"] = round(elapsed, 1)
            trace["effective_interval_sec"] = round(interval, 1)
            trace["pace_multiplier"] = round(multiplier, 3)
        # 1) hard floor — 어떤 신호로도 우회 불가한 절대 하한.
        if elapsed < self.fire_interval_floor_sec:
            return "global_throttle"
        # 2) 적응 간격 — 지났으면 통과.
        if elapsed >= interval:
            return None
        # 3) 조기 해제 — 직전 발화 이후 의미 있는 새 편집이 있으면 floor만으로 허용.
        if self._has_new_content_locked(state, doc_chars=doc_chars):
            if trace is not None:
                trace["early_release"] = True
            return None
        return "adaptive_interval"

    def _effective_interval_locked(
        self, state: ScenarioSchedulerState, *, now: float
    ) -> tuple[float, float]:
        multiplier = self._decayed_multiplier_locked(state, now=now)
        interval = self.fire_interval_base_sec * multiplier
        interval = max(
            self.fire_interval_floor_sec, min(self.fire_interval_ceil_sec, interval)
        )
        return interval, multiplier

    def _decayed_multiplier_locked(
        self, state: ScenarioSchedulerState, *, now: float
    ) -> float:
        multiplier = float(state.fire_pace_multiplier or 1.0)
        if multiplier <= 0:
            return 1.0
        if (
            multiplier == 1.0
            or state.pace_updated_at <= 0
            or self.pace_decay_half_life_sec <= 0
        ):
            return multiplier
        elapsed = max(0.0, now - state.pace_updated_at)
        return 1.0 + (multiplier - 1.0) * (
            0.5 ** (elapsed / self.pace_decay_half_life_sec)
        )

    def _has_new_content_locked(
        self, state: ScenarioSchedulerState, *, doc_chars: int | None
    ) -> bool:
        if self.early_release_min_new_chars <= 0:
            return False
        if doc_chars is None:
            return False
        if state.last_global_fire_doc_chars < 0:
            # 기준 없음(레거시/리셋 직후) — 허용 쪽으로.
            return True
        return (
            abs(int(doc_chars) - state.last_global_fire_doc_chars)
            >= self.early_release_min_new_chars
        )

    def _record_global_fire_baseline_locked(
        self,
        state: ScenarioSchedulerState,
        *,
        doc_chars: int | None,
        paragraph_fingerprint: str | None,
    ) -> None:
        state.last_global_fire_doc_chars = (
            int(doc_chars) if doc_chars is not None else -1
        )
        state.last_global_fire_paragraph_fp = str(paragraph_fingerprint or "")

    def record_card_outcome(
        self, document_key: str, outcome: str, *, now: float | None = None
    ) -> None:
        """카드 1장에 대한 사용자 반응을 페이스 multiplier에 반영한다.

        outcome ∈ {accept, retry, reject, ignore}. 그 외 값은 no-op. 누적 전에
        현재 시점까지의 감쇠를 먼저 접어 넣으므로(consecutive feedback이 감쇠된
        값 위에 곱해짐) 오래전 거절이 과대 반영되지 않는다."""
        factor = self.PACE_FEEDBACK_FACTORS.get(str(outcome or "").strip().lower())
        if factor is None:
            return
        now = now if now is not None else time.time()
        with self._lock:
            state = self.get_state(document_key, now=now)
            current = self._decayed_multiplier_locked(state, now=now)
            multiplier_max = max(
                1.0,
                self.fire_interval_ceil_sec / max(self.fire_interval_base_sec, 1e-6),
            )
            state.fire_pace_multiplier = min(
                max(current * factor, self._PACE_MULTIPLIER_MIN), multiplier_max
            )
            state.pace_updated_at = now

    def is_paragraph_throttled(
        self,
        document_key: str,
        paragraph_fingerprint: str | None,
        *,
        now: float | None = None,
    ) -> bool:
        """True when *any* scenario fired on this paragraph within
        ``paragraph_cooldown_sec``. Router-path counterpart of the inline check
        in :meth:`select_and_charge`."""
        now = now if now is not None else time.time()
        with self._lock:
            state = self.get_state(document_key, now=now)
            return self._paragraph_throttled_locked(state, paragraph_fingerprint, now=now)

    # 단락 fingerprint 발동 기록 상한 — 한 문서에서 오래된 단락 기록이 무한히 쌓이지 않게.
    _PARAGRAPH_FIRE_HISTORY_MAX = 32

    def _paragraph_throttled_locked(
        self,
        state: ScenarioSchedulerState,
        paragraph_fingerprint: str | None,
        *,
        now: float,
    ) -> bool:
        if self.paragraph_cooldown_sec <= 0 or not paragraph_fingerprint:
            return False
        last_at = state.last_fired_paragraphs.get(str(paragraph_fingerprint))
        if last_at is None:
            return False
        return (now - last_at) < self.paragraph_cooldown_sec

    def _record_paragraph_fire_locked(
        self,
        state: ScenarioSchedulerState,
        paragraph_fingerprint: str | None,
        *,
        now: float,
    ) -> None:
        if not paragraph_fingerprint:
            return
        state.last_fired_paragraphs[str(paragraph_fingerprint)] = now
        if len(state.last_fired_paragraphs) > self._PARAGRAPH_FIRE_HISTORY_MAX:
            ranked = sorted(
                state.last_fired_paragraphs.items(), key=lambda item: item[1], reverse=True
            )
            state.last_fired_paragraphs = dict(
                ranked[: self._PARAGRAPH_FIRE_HISTORY_MAX]
            )

    def record_fire(
        self,
        document_key: str,
        name: str,
        *,
        now: float | None = None,
        doc_chars: int | None = None,
        paragraph_fingerprint: str | None = None,
    ) -> None:
        """Record that ``name`` fired: updates recency (last_fired_at) + the global
        throttle anchor, WITHOUT charging vruntime. The router, not vruntime, owns
        selection, so per-scenario cooldown gates and the throttle still work while
        the fairness machinery stays dormant."""
        now = now if now is not None else time.time()
        with self._lock:
            state = self.get_state(document_key, now=now)
            state.last_fired_at[name] = now
            state.last_global_fire_at = now
            state.last_activity_at = now
            if doc_chars is not None:
                state.last_fired_doc_chars[name] = doc_chars
            self._record_paragraph_fire_locked(state, paragraph_fingerprint, now=now)
            self._record_global_fire_baseline_locked(
                state, doc_chars=doc_chars, paragraph_fingerprint=paragraph_fingerprint
            )

    def allow_immediate_fire(
        self,
        document_key: str,
        *,
        scenario_name: str | None = None,
        paragraph_fingerprint: str | None = None,
        now: float | None = None,
    ) -> None:
        """사용자가 카드에 '다시'(retry)를 눌렀을 때 호출 — 다음 캡처가 곧바로
        새 제안을 만들 수 있게 이 문서의 발화 브레이크를 선별적으로 푼다:
        전역 throttle 기준점, 해당 단락의 cross-scenario cooldown, 그리고 (알고
        있다면) 그 카드를 만든 시나리오의 자체 cooldown. 다른 시나리오/단락의
        cooldown은 건드리지 않는다."""
        now = now if now is not None else time.time()
        with self._lock:
            state = self.get_state(document_key, now=now)
            state.last_global_fire_at = 0.0
            if paragraph_fingerprint:
                state.last_fired_paragraphs.pop(str(paragraph_fingerprint), None)
            if scenario_name:
                state.last_fired_at.pop(str(scenario_name), None)

    def snapshot(self, document_key: str, *, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        state = self.get_state(document_key, now=now)
        interval, multiplier = self._effective_interval_locked(state, now=now)
        return {
            "document_key": state.document_key,
            "vruntimes": dict(state.vruntimes),
            "initial_vruntimes": dict(state.initial_vruntimes),
            "last_decay_at": state.last_decay_at,
            "last_activity_at": state.last_activity_at,
            "last_reset_at": state.last_reset_at,
            "last_fired_at": dict(state.last_fired_at),
            "last_fired_doc_chars": dict(state.last_fired_doc_chars),
            "last_global_fire_at": state.last_global_fire_at,
            "fire_interval_floor_sec": self.fire_interval_floor_sec,
            "fire_interval_base_sec": self.fire_interval_base_sec,
            "fire_interval_ceil_sec": self.fire_interval_ceil_sec,
            "fire_pace_multiplier": round(multiplier, 3),
            "effective_fire_interval_sec": round(interval, 1),
            "last_fired_paragraphs": dict(state.last_fired_paragraphs),
            "paragraph_cooldown_sec": self.paragraph_cooldown_sec,
        }

    def flush_all(self) -> None:
        with self._lock:
            for document_key, state in list(self._cache.items()):
                try:
                    self.store.save_scheduler_state(document_key, state.to_payload())
                except OSError as exc:
                    if self.console_log:
                        print(f"[screen_context][scheduler][warn] flush failed for {document_key}: {exc}")
            self._prune_locked()

    def _flush_loop(self) -> None:
        while not self._stop_event.wait(self.flush_interval_sec):
            try:
                self.flush_all()
            except Exception as exc:
                if self.console_log:
                    print(f"[screen_context][scheduler][error] flush_loop: {type(exc).__name__}: {exc}")

    def _prune_locked(self) -> None:
        """Evict in-memory cache + delete disk files past max_documents (LRU).

        Must be called with self._lock held, AFTER the main flush loop has
        already persisted every cached state. Cache ranking uses
        last_activity_at; disk ranking uses file mtime as a fallback for
        files that have no in-cache counterpart (e.g. orphaned states from
        prior sessions).
        """
        # 1) Evict cache entries past the cap (oldest last_activity_at first).
        # Their on-disk copies remain — already written by the preceding
        # flush_all() main loop. Do NOT re-save here: that would refresh
        # mtimes for evicted entries and invert the LRU ordering on disk.
        if len(self._cache) > self.max_documents:
            ranked = sorted(
                self._cache.items(),
                key=lambda item: item[1].last_activity_at,
                reverse=True,
            )
            keep_keys = {key for key, _ in ranked[: self.max_documents]}
            for key in list(self._cache.keys()):
                if key not in keep_keys:
                    self._cache.pop(key, None)

        # 2) Prune disk files past the cap. Files whose document_key is
        # currently in cache are always kept; only orphan files compete for
        # the remaining slots, ranked by mtime descending.
        self._prune_disk_only()

    def _prune_disk_only(self) -> None:
        try:
            all_files = list(self.store.scheduler_dir.glob("*.json"))
        except OSError:
            return

        cached_paths = {
            self.store.scheduler_state_path(key).resolve()
            for key in self._cache.keys()
        }
        cached_files: list = []
        orphan_files: list = []
        for path in all_files:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in cached_paths:
                cached_files.append(path)
            else:
                orphan_files.append(path)

        # Cached entries are always kept; orphans fill the remaining slots
        # ordered by mtime descending so newer past-session state survives.
        remaining_slots = max(self.max_documents - len(cached_files), 0)
        try:
            orphan_files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            return
        to_delete = orphan_files[remaining_slots:]
        for path in to_delete:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                if self.console_log:
                    print(f"[screen_context][scheduler][warn] prune failed: {path.name}")

    def _load_or_create(self, document_key: str, *, now: float) -> ScenarioSchedulerState:
        payload = self.store.load_scheduler_state(document_key)
        if payload:
            state = ScenarioSchedulerState.from_payload(payload)
            if not state.document_key:
                state.document_key = document_key
            if not state.last_decay_at:
                state.last_decay_at = now
            if not state.last_activity_at:
                state.last_activity_at = now
            if not state.last_reset_at:
                state.last_reset_at = now
            return state
        return ScenarioSchedulerState(
            document_key=document_key,
            initial_vruntimes={name: w.initial_vruntime for name, w in self.weights.items()},
            vruntimes={name: w.initial_vruntime for name, w in self.weights.items()},
            last_decay_at=now,
            last_activity_at=now,
            last_reset_at=now,
        )

    def _maybe_reset(self, state: ScenarioSchedulerState, *, now: float) -> None:
        idle_elapsed = now - state.last_activity_at
        absolute_elapsed = now - state.last_reset_at
        idle_reset = self.reset_idle_sec > 0 and idle_elapsed >= self.reset_idle_sec
        absolute_reset = self.reset_interval_sec > 0 and absolute_elapsed >= self.reset_interval_sec
        if not (idle_reset or absolute_reset):
            return
        for name, weight in self.weights.items():
            state.vruntimes[name] = weight.initial_vruntime
            state.initial_vruntimes[name] = weight.initial_vruntime
        # 발동 기록 초기화 → 모든 cooldown 면제 (전역 throttle + 단락 cooldown 포함)
        state.last_fired_at.clear()
        state.last_fired_doc_chars.clear()
        state.last_global_fire_at = 0.0
        state.last_fired_paragraphs.clear()
        state.fire_pace_multiplier = 1.0
        state.pace_updated_at = 0.0
        state.last_global_fire_doc_chars = -1
        state.last_global_fire_paragraph_fp = ""
        state.last_decay_at = now
        state.last_activity_at = now
        state.last_reset_at = now

    def _apply_decay(self, state: ScenarioSchedulerState, *, now: float) -> None:
        elapsed = now - state.last_decay_at
        if elapsed <= 0 or self.decay_per_second <= 0:
            state.last_decay_at = now
            return
        decay_amount = elapsed * self.decay_per_second
        for name in list(state.vruntimes.keys()):
            initial = state.initial_vruntimes.get(name, self.weights.get(name, ScenarioWeights(0.0, 0.0)).initial_vruntime)
            current = state.vruntimes[name]
            state.vruntimes[name] = max(current - decay_amount, initial)
        state.last_decay_at = now

    def _sync_known_scenarios(self, state: ScenarioSchedulerState) -> None:
        """Ensure freshly added scenarios appear in persisted state."""
        for name, weight in self.weights.items():
            if name not in state.vruntimes:
                state.vruntimes[name] = weight.initial_vruntime
            if name not in state.initial_vruntimes:
                state.initial_vruntimes[name] = weight.initial_vruntime
