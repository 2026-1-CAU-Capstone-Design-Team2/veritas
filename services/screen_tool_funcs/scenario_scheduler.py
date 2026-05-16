from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .scenario import ScenarioType
from .store import ScreenContextStore


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
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ScenarioSchedulerState":
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
        min_global_fire_interval_sec: float = 30.0,
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
        # 시나리오 무관 발화 간 최소 간격. 0 이하면 throttle 비활성.
        self.min_global_fire_interval_sec = max(min_global_fire_interval_sec, 0.0)
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
            "elapsed_since_last_fire_sec": None,
            "min_interval_sec": self.min_global_fire_interval_sec,
            "last_global_fire_at": 0.0,
        }
        trace["rejected_reason"] = None

        if not ready_names:
            trace["rejected_reason"] = "no_ready_candidates"
            return None
        now = now if now is not None else time.time()
        with self._lock:
            state = self.get_state(document_key, now=now)
            trace["global_throttle"]["last_global_fire_at"] = state.last_global_fire_at
            # 시나리오 무관 전역 rate-limit — 직전 발동으로부터 일정 시간 안엔 누구도 발화 X
            if self.min_global_fire_interval_sec > 0 and state.last_global_fire_at > 0:
                elapsed = now - state.last_global_fire_at
                trace["global_throttle"]["elapsed_since_last_fire_sec"] = round(elapsed, 1)
                if elapsed < self.min_global_fire_interval_sec:
                    trace["global_throttle"]["active"] = True
                    trace["rejected_reason"] = "global_throttle"
                    if self.console_log:
                        print(
                            "[screen_context][scheduler] "
                            f"global throttle: {elapsed:.1f}s < {self.min_global_fire_interval_sec:.1f}s, "
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
            trace["selected"] = selected
            trace["selected_vruntime_before"] = round(current, 4)
            trace["selected_vruntime_after"] = round(new_vruntime, 4)
            return selected

    def snapshot(self, document_key: str, *, now: float | None = None) -> dict[str, Any]:
        state = self.get_state(document_key, now=now)
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
            "min_global_fire_interval_sec": self.min_global_fire_interval_sec,
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
        # 발동 기록 초기화 → 모든 cooldown 면제 (전역 throttle 포함)
        state.last_fired_at.clear()
        state.last_fired_doc_chars.clear()
        state.last_global_fire_at = 0.0
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
