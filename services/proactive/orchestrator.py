"""ProactiveOrchestrator — rule-based pipeline.

The single entry point for both writing surfaces. Per-observe flow:

    1. Extract primitive signals (cheap rolling telemetry)
    2. Extract ActiveAnchor (cursor/selection/paragraph/section)
    3. CandidateFactory: anchor-local task candidates (≤3)
    4. For each candidate: hard gates → rubric score
    5. Pick top candidate if its score ≥ adjusted_threshold; else NullPrediction
    6. Persist decision log (no raw text)
    7. Stash decision + raw text in in-memory cache (for generate/explain)
    8. Register render_timeout (task) or null_outcome (null) pending entry

Per-feedback flow:

    1. Map raw_action → canonical
    2. UserAdaptationMemory.apply_feedback(...)
    3. Drop pending timeout entry
    4. Log feedback row

No bandit. No θ̂. No UCB. Just deterministic gates + rubric + lightweight
threshold/cooldown/suppression memory.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional


# ----------------------------------------------------------- per-anchor reject ladder
#
# Tracked in-memory only (see services/proactive/README.md §"Native reject
# ladder"). NOT persisted — this is a per-session UX gate, not learned state.
# Three-strike rule:
#   1st reject at anchor X → next attempt expands context (reject_level=1)
#   2nd reject at anchor X → broader still (reject_level=2)
#   3rd reject at anchor X → 180s per-anchor cooldown set
# Other anchors are unaffected by this state; returning to a cool-down anchor
# does NOT reset the cooldown (the cooldown is only cleared by an accept).

NATIVE_ANCHOR_REJECT_LIMIT: int = 3
NATIVE_ANCHOR_REJECT_COOLDOWN_S: float = 180.0


@dataclass
class _AnchorRejectState:
    reject_count: int = 0
    cooldown_until_monotonic: Optional[float] = None
    last_rejected_text: Optional[str] = None

from .anchors import ActiveAnchor, compute_anchor_id, confidence_from_source
from .candidates import PrimitiveSignals, build_candidates
from .context_selector import ContextBundle, materialize_context
from .evaluator import (
    GateResult,
    ScoreBreakdown,
    adjusted_threshold,
    check_hard_gates,
    score_candidate,
)
from .features import extract_primitive_features
from .generator import ProactiveGenerator
from .models import ProactiveObservation
from .null_outcome_monitor import (
    NULL_OUTCOME_HORIZON_SECONDS,
    NullOutcomeMonitor,
    classify_null_outcome,
)
from .policy_store import ProactiveStore
from .proposal_models import (
    NullPrediction,
    Prediction,
    ProactiveTask,
    SurfaceCapabilities,
    is_task,
)
from .reward import canonicalize_feedback
from .telemetry import get_telemetry, release_telemetry
from .timeout_monitor import (
    TimeoutMonitor,
    now_plus as render_now_plus,
    render_timeout_seconds,
)

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ----------------------------------------------------------- doc tracking


class _DocumentTrack:
    """Rolling per-document telemetry derived from the observe stream."""

    def __init__(self, *, window_seconds: float = 30.0, history_max: int = 32) -> None:
        self._lock = threading.Lock()
        self._history: deque[tuple[float, str]] = deque(maxlen=history_max)
        self._last_mutation_ts: float = time.monotonic()
        self._last_intervention_ts: Optional[float] = None
        self._last_text: str = ""
        self._stable_count: int = 0
        self.window_seconds: float = float(window_seconds)
        # Sticky flags for the candidate factory's recent_* signals.
        self._recent_undo_until: float = 0.0
        self._recent_paste_until: float = 0.0
        self._recent_large_delete_until: float = 0.0

    def observe(self, *, text: str, captured_ts: float) -> dict[str, Any]:
        with self._lock:
            added = max(0, len(text) - len(self._last_text))
            deleted = max(0, len(self._last_text) - len(text))
            mutation = text != self._last_text

            if mutation:
                self._last_mutation_ts = captured_ts
                self._stable_count = 0
            else:
                self._stable_count += 1

            cutoff = captured_ts - self.window_seconds
            while self._history and self._history[0][0] < cutoff:
                self._history.popleft()
            self._history.append((captured_ts, text))

            added_window = added
            deleted_window = deleted
            if len(self._history) >= 2:
                added_window = 0
                deleted_window = 0
                prev = self._history[0][1]
                for _ts, cur in list(self._history)[1:]:
                    added_window += max(0, len(cur) - len(prev))
                    deleted_window += max(0, len(prev) - len(cur))
                    prev = cur

            idle_sec = max(0.0, captured_ts - self._last_mutation_ts)
            time_since_last = (
                captured_ts - self._last_intervention_ts
                if self._last_intervention_ts is not None
                else 9999.0
            )

            # Flag detectors — sticky for 8 seconds so a single observe tick
            # after the action still sees them set.
            if deleted >= 80:
                self._recent_large_delete_until = captured_ts + 8.0
            if added >= 80:
                self._recent_paste_until = captured_ts + 8.0
            recent_large_delete = captured_ts <= self._recent_large_delete_until
            recent_paste = captured_ts <= self._recent_paste_until
            recent_undo = captured_ts <= self._recent_undo_until

            self._last_text = text
            return {
                "idle_sec": float(idle_sec),
                "stable_capture_count": int(self._stable_count),
                "added_chars_window": float(added_window),
                "deleted_chars_window": float(deleted_window),
                "time_since_last_intervention": float(time_since_last),
                "recent_large_delete": bool(recent_large_delete),
                "recent_paste": bool(recent_paste),
                "recent_undo": bool(recent_undo),
            }

    def mark_intervention(self, ts: Optional[float] = None) -> None:
        with self._lock:
            self._last_intervention_ts = float(ts if ts is not None else time.monotonic())

    def snapshot_volume(self) -> tuple[float, float, float]:
        with self._lock:
            if not self._history:
                return 0.0, 0.0, 0.0
            added = 0
            deleted = 0
            prev = self._history[0][1]
            for _ts, cur in list(self._history)[1:]:
                added += max(0, len(cur) - len(prev))
                deleted += max(0, len(prev) - len(cur))
                prev = cur
            vol = float(added + deleted)
            net = float(added - deleted)
            from .features import clip01

            churn = clip01(vol / 300.0) * (1.0 - clip01(abs(net) / (vol + 1e-6)))
            idle = time.monotonic() - self._last_mutation_ts
            return vol, float(churn), float(idle)


# ----------------------------------------------------------- anchor extract


def _extract_anchor(observation: ProactiveObservation) -> ActiveAnchor:
    """Best-effort ActiveAnchor from the observation payload.

    Native editor observations carry cursor + slices directly. External
    captures carry whatever the screen pipeline managed to read; the
    source defaults to ``ocr_visible_text`` so the confidence stays low
    when UIA caret data wasn't available.
    """
    surface_kind = (
        "native_editor"
        if observation.surface == "native_editor"
        else "external_app"
    )
    has_cursor = observation.cursor_index is not None
    has_selection = bool(
        (observation.metadata or {}).get("selection_start") is not None
        and (observation.metadata or {}).get("selection_end") is not None
    )
    has_paragraph = bool(observation.current_paragraph)
    has_section = bool((observation.metadata or {}).get("section_heading"))

    if observation.surface == "native_editor":
        source = "native_selection" if has_selection else ("native_cursor" if has_cursor else "unknown")
    else:
        # Caller may flag "uia_caret" / "uia_selection" via metadata; otherwise
        # we conservatively assume OCR-only.
        m = observation.metadata or {}
        source = str(m.get("anchor_source") or ("uia_caret" if has_cursor else "ocr_visible_text"))

    confidence = confidence_from_source(
        source=source,  # type: ignore[arg-type]
        has_cursor=has_cursor or has_selection,
        has_paragraph=has_paragraph,
        has_section=has_section,
    )

    return ActiveAnchor(
        document_id=str(observation.document_id or observation.document_key or ""),
        surface=surface_kind,  # type: ignore[arg-type]
        cursor_index=observation.cursor_index,
        selection_start=(observation.metadata or {}).get("selection_start"),
        selection_end=(observation.metadata or {}).get("selection_end"),
        sentence_text=observation.current_sentence or None,
        paragraph_text=observation.current_paragraph or None,
        section_heading=(observation.metadata or {}).get("section_heading"),
        prev_sentence=(observation.metadata or {}).get("prev_sentence"),
        next_sentence=(observation.metadata or {}).get("next_sentence"),
        prev_paragraph=observation.previous_paragraph or None,
        next_paragraph=(observation.metadata or {}).get("next_paragraph"),
        confidence=confidence,
        source=source,  # type: ignore[arg-type]
    )


def _surface_caps(observation: ProactiveObservation) -> SurfaceCapabilities:
    """Surface capability flags. Frontend can pass overrides via
    metadata.surface_capabilities; defaults match the current Qt UI:
    native ghost is implemented, inline_diff / inline_marker are not yet."""
    if observation.surface == "native_editor":
        m = (observation.metadata or {}).get("surface_capabilities") or {}
        return SurfaceCapabilities.for_native(
            inline_diff=bool(m.get("native_inline_diff", False)),
            inline_marker=bool(m.get("native_inline_marker", False)),
        )
    return SurfaceCapabilities.for_external()


# ----------------------------------------------------------- orchestrator


class ProactiveOrchestrator:
    """Per-workspace orchestrator. One instance per AgentRuntime binding."""

    def __init__(
        self,
        *,
        output_root: Path,
        workspace_id: str,
        generator: ProactiveGenerator,
    ) -> None:
        self.output_root = Path(output_root)
        self.workspace_id = workspace_id
        self.store = ProactiveStore(output_root=self.output_root, workspace_id=workspace_id)
        self.generator = generator
        self._telemetry = get_telemetry(
            workspace_id=workspace_id,
            log_dir=self.store.policy_dir,
        )

        # decision_id → {prediction, anchor, context_bundle, observation, ...}
        self._decision_cache: dict[str, dict[str, Any]] = {}
        self._decision_order: deque[str] = deque(maxlen=512)

        self._tracks: dict[str, _DocumentTrack] = {}
        self._tracks_lock = threading.Lock()

        # In-memory per-anchor reject ladder for native_editor.
        # Documented in services/proactive/README.md §"Native reject ladder".
        # Deliberately NOT persisted — closing/reopening the editor resets it.
        self._anchor_reject_state: dict[str, _AnchorRejectState] = {}
        self._anchor_reject_lock = threading.Lock()

        self._render_timeout_monitor = TimeoutMonitor(
            on_render_timeout=self._resolve_render_timeout,
            get_pending=self._read_pending_render,
            set_pending=self._write_pending_render,
        )
        self._null_outcome_monitor = NullOutcomeMonitor(
            on_resolve=self._resolve_null_outcome,
            get_pending=self._read_pending_null,
            set_pending=self._write_pending_null,
        )
        self._render_timeout_monitor.start()
        self._null_outcome_monitor.start()

    # ----------------------------------------------------------- lifecycle

    def close(self) -> None:
        try:
            self._render_timeout_monitor.stop()
        except Exception:
            pass
        try:
            self._null_outcome_monitor.stop()
        except Exception:
            pass
        try:
            self.store.adaptation.save()
        except Exception:
            pass
        try:
            release_telemetry(self.store.policy_dir)
        except Exception:
            pass

    # ----------------------------------------------------------- observe

    def _get_track(self, document_key: str) -> _DocumentTrack:
        key = document_key or "_default_"
        with self._tracks_lock:
            track = self._tracks.get(key)
            if track is None:
                track = _DocumentTrack()
                self._tracks[key] = track
            return track

    def _stash(
        self,
        *,
        decision_id: str,
        prediction: Prediction,
        anchor: ActiveAnchor,
        observation: ProactiveObservation,
        context: Optional[ContextBundle],
        primitive: dict[str, Any],
        candidate_count: int,
        evaluator_breakdown: Optional[ScoreBreakdown],
        threshold: float,
        gate_reasons: list[str],
    ) -> None:
        if len(self._decision_order) == self._decision_order.maxlen:
            old = self._decision_order[0]
            self._decision_cache.pop(old, None)
        self._decision_order.append(decision_id)
        self._decision_cache[decision_id] = {
            "decision_id": decision_id,
            "prediction": prediction,
            "anchor": anchor,
            "observation": observation,
            "context": context,
            "primitive": primitive,
            "candidate_count": candidate_count,
            "evaluator_breakdown": evaluator_breakdown,
            "threshold": threshold,
            "gate_reasons": list(gate_reasons),
            "stored_at": time.monotonic(),
            "created_at": _now_iso(),
        }

    def get_decision(self, decision_id: str) -> Optional[dict[str, Any]]:
        return self._decision_cache.get(decision_id)

    def observe(self, observation: ProactiveObservation) -> dict[str, Any]:
        """Run the rule-based observe pipeline. Returns a dict with the
        decision_id and the prediction (task or null) ready for HTTP layer
        to project to its camelCase response shape."""
        captured_ts = time.monotonic()
        track = self._get_track(observation.document_key)
        rolling = track.observe(text=observation.text, captured_ts=captured_ts)

        anchor = _extract_anchor(observation)

        # Per-anchor reject ladder check (native only).
        # This MUST happen before the candidate factory + evaluator chain so
        # we don't waste cycles building a candidate that's only going to be
        # vetoed on a per-anchor cooldown.
        decision_id_early = f"pd_{uuid.uuid4().hex[:16]}"
        if observation.surface == "native_editor":
            reject_level, last_rejected, in_cooldown = self._read_anchor_state(
                anchor.anchor_id
            )
            if in_cooldown:
                # Compute primitive minimally so the null log still has the
                # primitive snapshot operators expect.
                primitive_cooldown = extract_primitive_features(
                    observation=observation,
                    idle_sec=float(rolling["idle_sec"]),
                    stable_capture_count=int(rolling["stable_capture_count"]),
                    added_chars_window=float(rolling["added_chars_window"]),
                    deleted_chars_window=float(rolling["deleted_chars_window"]),
                    recent_negative_rate=float(
                        self.store.adaptation.state.global_stats.recent_negative_rate
                    ),
                    time_since_last_intervention=float(rolling["time_since_last_intervention"]),
                    relevant_sources_available=self._workspace_has_sources(),
                )
                return self._emit_null(
                    decision_id=decision_id_early,
                    observation=observation,
                    anchor=anchor,
                    primitive=primitive_cooldown,
                    reason="anchor_reject_cooldown",
                    gate_reasons=["per_anchor_3_reject_cooldown"],
                    candidate_count=0,
                    threshold=float("inf"),
                )
        else:
            reject_level, last_rejected = 0, None

        primitive = extract_primitive_features(
            observation=observation,
            idle_sec=float(rolling["idle_sec"]),
            stable_capture_count=int(rolling["stable_capture_count"]),
            added_chars_window=float(rolling["added_chars_window"]),
            deleted_chars_window=float(rolling["deleted_chars_window"]),
            recent_negative_rate=float(
                self.store.adaptation.state.global_stats.recent_negative_rate
            ),
            time_since_last_intervention=float(rolling["time_since_last_intervention"]),
            relevant_sources_available=self._workspace_has_sources(),
        )
        # Add the diff-related flags into the primitive dict so the candidate
        # factory can pick them up.
        primitive["recent_large_delete"] = bool(rolling.get("recent_large_delete"))
        primitive["recent_paste"] = bool(rolling.get("recent_paste"))
        primitive["recent_undo"] = bool(rolling.get("recent_undo"))
        primitive["recent_diff_overlaps_anchor"] = bool(
            (observation.metadata or {}).get("recent_diff_overlaps_anchor", False)
        )

        signals = PrimitiveSignals.from_primitive(primitive)
        surface = _surface_caps(observation)
        candidates = build_candidates(
            anchor=anchor,
            signals=signals,
            surface=surface,
            user_adaptation=self.store.adaptation.state,
        )

        decision_id = f"pd_{uuid.uuid4().hex[:16]}"

        if not candidates:
            return self._emit_null(
                decision_id=decision_id,
                observation=observation,
                anchor=anchor,
                primitive=primitive,
                reason=(
                    "low_anchor_confidence"
                    if not anchor.is_active_suggestion_capable()
                    else "no_candidate"
                ),
                gate_reasons=[],
                candidate_count=0,
                threshold=adjusted_threshold(
                    task_type="next_sentence",
                    anchor_id=anchor.anchor_id,
                    user_adaptation=self.store.adaptation.state,
                ),
            )

        # Score each candidate; keep gate failures alongside for /explain.
        scored: list[tuple[ProactiveTask, GateResult, Optional[ScoreBreakdown], float]] = []
        for candidate in candidates:
            gate = check_hard_gates(
                candidate=candidate,
                anchor=anchor,
                signals=signals,
                surface=surface,
                user_adaptation=self.store.adaptation.state,
            )
            if not gate.allowed:
                scored.append((candidate, gate, None, 0.0))
                continue
            breakdown = score_candidate(
                candidate=candidate,
                anchor=anchor,
                signals=signals,
                user_adaptation=self.store.adaptation.state,
            )
            scored.append((candidate, gate, breakdown, breakdown.total))

        # Pick the best gate-passing candidate.
        passing = [(c, b, s) for (c, g, b, s) in scored if g.allowed and b is not None]
        passing.sort(key=lambda t: t[2], reverse=True)

        if not passing:
            # Every candidate hard-gated. Surface the union of gate reasons.
            gate_reasons = sorted({r for (_c, g, _b, _s) in scored for r in g.reasons})
            return self._emit_null(
                decision_id=decision_id,
                observation=observation,
                anchor=anchor,
                primitive=primitive,
                reason="all_candidates_gated",
                gate_reasons=gate_reasons,
                candidate_count=len(candidates),
                threshold=adjusted_threshold(
                    task_type=candidates[0].task_type,
                    anchor_id=anchor.anchor_id,
                    user_adaptation=self.store.adaptation.state,
                ),
            )

        best_task, best_breakdown, best_score = passing[0]
        threshold = adjusted_threshold(
            task_type=best_task.task_type,
            anchor_id=anchor.anchor_id,
            user_adaptation=self.store.adaptation.state,
        )

        if best_score < threshold:
            # When the threshold is ``+inf`` the candidate didn't lose on
            # score quality — it lost because the (anchor, task) pair is
            # on cooldown or the task type is suppressed. Surface that
            # distinction in the reason so the operator can tell "this
            # would have shown if not for adaptation" apart from "this
            # candidate was just too weak".
            if threshold == float("inf"):
                reason = "blocked_by_cooldown_or_suppression"
            else:
                reason = "score_below_threshold"
            return self._emit_null(
                decision_id=decision_id,
                observation=observation,
                anchor=anchor,
                primitive=primitive,
                reason=reason,
                gate_reasons=[],
                candidate_count=len(candidates),
                threshold=threshold,
                best_score=best_score,
            )

        # Task chosen — materialize context, log, register render timeout.
        best_task.evaluator_score = best_score
        # Attach the per-anchor reject ladder state into task metadata. The
        # generator uses these for the native-retry path so the LLM gets the
        # previous (rejected) suggestion as a negative example, plus an
        # explicit instruction to vary on reject_level ≥ 1.
        if observation.surface == "native_editor" and reject_level > 0:
            best_task.metadata["reject_level"] = int(reject_level)
            if last_rejected:
                best_task.metadata["last_rejected_text"] = str(last_rejected)
        context_bundle = materialize_context(task=best_task, anchor=anchor)

        track.mark_intervention(captured_ts)
        self.store.adaptation.mark_intervention_shown()

        self._stash(
            decision_id=decision_id,
            prediction=best_task,
            anchor=anchor,
            observation=observation,
            context=context_bundle,
            primitive=primitive,
            candidate_count=len(candidates),
            evaluator_breakdown=best_breakdown,
            threshold=threshold,
            gate_reasons=[],
        )
        self._log_decision_task(
            decision_id=decision_id,
            task=best_task,
            anchor=anchor,
            primitive=primitive,
            context_meta=context_bundle.meta_dict(),
            evaluator_breakdown=best_breakdown,
            threshold=threshold,
            candidate_count=len(candidates),
        )
        self._register_render_timeout(decision_id, best_task)
        self._telemetry.decision_task(
            decision_id=decision_id,
            surface=observation.surface,
            task_type=best_task.task_type,
            anchor_id=anchor.anchor_id,
            anchor_confidence=anchor.confidence,
            context_scope=best_task.context_scope,
            render_mode=best_task.render_mode,
            evaluator_score=best_score,
            threshold=threshold,
            candidate_count=len(candidates),
            primitive=primitive,
        )

        return {
            "decision_id": decision_id,
            "prediction": "task",
            "task": best_task,
            "anchor": anchor,
            "evaluator_score": best_score,
            "threshold": threshold,
            "candidate_count": len(candidates),
        }

    def _emit_null(
        self,
        *,
        decision_id: str,
        observation: ProactiveObservation,
        anchor: ActiveAnchor,
        primitive: dict[str, Any],
        reason: str,
        gate_reasons: list[str],
        candidate_count: int,
        threshold: float,
        best_score: Optional[float] = None,
    ) -> dict[str, Any]:
        null_pred = NullPrediction(
            reason=reason,
            gate_reasons=list(gate_reasons),
            evaluator_score=float(best_score or 0.0),
            candidate_count=candidate_count,
        )
        self._stash(
            decision_id=decision_id,
            prediction=null_pred,
            anchor=anchor,
            observation=observation,
            context=None,
            primitive=primitive,
            candidate_count=candidate_count,
            evaluator_breakdown=None,
            threshold=threshold,
            gate_reasons=gate_reasons,
        )
        self._log_decision_null(
            decision_id=decision_id,
            null=null_pred,
            anchor=anchor,
            primitive=primitive,
            threshold=threshold,
        )
        self._register_null_outcome(decision_id, anchor)
        self._telemetry.decision_null(
            decision_id=decision_id,
            surface=observation.surface,
            reason=reason,
            candidate_count=candidate_count,
            gate_reasons=gate_reasons,
            best_score=best_score,
            threshold=threshold,
        )
        return {
            "decision_id": decision_id,
            "prediction": "null",
            "null": null_pred,
            "anchor": anchor,
            "threshold": threshold,
            "candidate_count": candidate_count,
        }

    # ----------------------------------------------------------- generate

    def stream_generation(self, decision_id: str) -> Iterator[dict[str, Any]]:
        bundle = self._decision_cache.get(decision_id)
        if bundle is None:
            return iter(())
        prediction = bundle["prediction"]
        if not is_task(prediction):
            return iter(())
        task: ProactiveTask = prediction  # type: ignore[assignment]
        context: ContextBundle = bundle["context"]
        observation: ProactiveObservation = bundle["observation"]
        # Pass the observation so the generator can use raw prefix/suffix for
        # native ghost mode — the context_selector's reconstructed parts often
        # fall below ChatAgent._is_continuation_moment's min-chars threshold.
        return self.generator.stream(
            decision_id=decision_id,
            task=task,
            context=context,
            workspace_id=observation.workspace_id,
            surface=observation.surface,
            observation=observation,
        )

    # ----------------------------------------------------------- feedback

    def record_feedback(
        self,
        *,
        decision_id: str,
        raw_action: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        bundle = self._decision_cache.get(decision_id)
        # We can record feedback even after the cache has rolled — we just
        # can't apply task/anchor-specific adaptation in that case.
        surface = "external_app"
        task_type: Optional[str] = None
        anchor_id: Optional[str] = None
        if bundle is not None:
            obs: ProactiveObservation = bundle["observation"]
            surface = obs.surface
            prediction = bundle["prediction"]
            if is_task(prediction):
                task_type = prediction.task_type  # type: ignore[union-attr]
                anchor_id = prediction.target_anchor_id  # type: ignore[union-attr]

        canonical = canonicalize_feedback(surface=surface, raw_action=raw_action)

        # --- Per-anchor reject ladder (native only, in-memory) ----------
        # Documented in services/proactive/README.md §"Native reject ladder".
        # We update this BEFORE calling adaptation.apply_feedback so any
        # downstream observe at the same anchor sees the new reject_level
        # immediately. The adaptation layer's per-(anchor,task) cooldown is
        # skipped for native — the ladder owns the gate now.
        meta = dict(metadata or {})
        generated_text = str(meta.get("generated_text") or "")
        if surface == "native_editor" and anchor_id:
            if canonical == "reject":
                self._bump_anchor_reject(anchor_id, generated_text)
            elif canonical == "retry":
                # Retry doesn't increment the count — the user wanted the
                # help, just not THIS rendition. But we DO need to remember
                # the rejected text so the next observe's generator can
                # tell the LLM "avoid this".
                self._remember_anchor_rejected_text(anchor_id, generated_text)
            elif canonical == "accept":
                # Accept clears the ladder entirely so the next observe at
                # this anchor gets a fresh attempt.
                self._clear_anchor_state(anchor_id)
        # ----------------------------------------------------------------

        changes = self.store.adaptation.apply_feedback(
            canonical=canonical,
            task_type=task_type,
            anchor_id=anchor_id,
            surface=surface,
        )
        self.store.log_feedback(
            {
                "decision_id": decision_id,
                "timestamp": _now_iso(),
                "prediction": "task" if task_type else "unknown",
                "task_type": task_type,
                "canonical_feedback": canonical,
                "raw_action": raw_action,
                "adaptation_changes": dict(changes),
                "metadata": dict(metadata or {}),
            }
        )
        self.store.log_update(
            {
                "decision_id": decision_id,
                "timestamp": _now_iso(),
                "canonical": canonical,
                "adaptation_changes": dict(changes),
            }
        )
        self._drop_pending(decision_id)
        self._telemetry.feedback(
            decision_id=decision_id,
            surface=surface,
            canonical=canonical,
            task_type=task_type,
            anchor_id=anchor_id,
            adaptation_changes=dict(changes),
        )
        return {
            "decision_id": decision_id,
            "surface": surface,
            "canonical_feedback": canonical,
            "task_type": task_type,
            "adaptation_changes": dict(changes),
        }

    # ----------------------------------------------------------- anchor ladder

    def _read_anchor_state(
        self, anchor_id: str
    ) -> tuple[int, Optional[str], bool]:
        """Return ``(reject_count, last_rejected_text, in_cooldown)`` for
        ``anchor_id``. All three are zero/None/False if the anchor has no
        ladder state yet."""
        with self._anchor_reject_lock:
            state = self._anchor_reject_state.get(anchor_id)
            if state is None:
                return 0, None, False
            in_cd = (
                state.cooldown_until_monotonic is not None
                and time.monotonic() < state.cooldown_until_monotonic
            )
            return state.reject_count, state.last_rejected_text, in_cd

    def _bump_anchor_reject(self, anchor_id: str, generated_text: str) -> None:
        with self._anchor_reject_lock:
            state = self._anchor_reject_state.setdefault(
                anchor_id, _AnchorRejectState()
            )
            state.reject_count += 1
            if generated_text:
                state.last_rejected_text = generated_text
            if state.reject_count >= NATIVE_ANCHOR_REJECT_LIMIT:
                state.cooldown_until_monotonic = (
                    time.monotonic() + NATIVE_ANCHOR_REJECT_COOLDOWN_S
                )

    def _remember_anchor_rejected_text(
        self, anchor_id: str, generated_text: str
    ) -> None:
        if not generated_text:
            return
        with self._anchor_reject_lock:
            state = self._anchor_reject_state.setdefault(
                anchor_id, _AnchorRejectState()
            )
            state.last_rejected_text = generated_text

    def _clear_anchor_state(self, anchor_id: str) -> None:
        with self._anchor_reject_lock:
            self._anchor_reject_state.pop(anchor_id, None)

    # ----------------------------------------------------------- pending

    def _read_pending_render(self) -> list[dict[str, Any]]:
        return [
            e for e in self.store.read_pending_timeouts()
            if str(e.get("kind") or "render_timeout") == "render_timeout"
        ]

    def _write_pending_render(self, kept: list[dict[str, Any]]) -> None:
        # Read full, swap render entries, write back so we don't drop null_outcome rows.
        all_pending = self.store.read_pending_timeouts()
        kept_render = list(kept)
        kept_others = [e for e in all_pending if str(e.get("kind") or "render_timeout") != "render_timeout"]
        self.store.write_pending_timeouts(kept_others + kept_render)

    def _read_pending_null(self) -> list[dict[str, Any]]:
        return [
            e for e in self.store.read_pending_timeouts()
            if str(e.get("kind") or "") == "null_outcome"
        ]

    def _write_pending_null(self, kept: list[dict[str, Any]]) -> None:
        all_pending = self.store.read_pending_timeouts()
        kept_others = [e for e in all_pending if str(e.get("kind") or "") != "null_outcome"]
        self.store.write_pending_timeouts(kept_others + list(kept))

    def _register_render_timeout(self, decision_id: str, task: ProactiveTask) -> None:
        pending = self.store.read_pending_timeouts()
        pending.append(
            {
                "kind": "render_timeout",
                "decision_id": decision_id,
                "expires_at": render_now_plus(render_timeout_seconds(task.render_mode)),
                "render_mode": task.render_mode,
                "task_type": task.task_type,
            }
        )
        self.store.write_pending_timeouts(pending)

    def _register_null_outcome(self, decision_id: str, anchor: ActiveAnchor) -> None:
        from .null_outcome_monitor import now_plus as null_now_plus

        pending = self.store.read_pending_timeouts()
        pending.append(
            {
                "kind": "null_outcome",
                "decision_id": decision_id,
                "expires_at": null_now_plus(NULL_OUTCOME_HORIZON_SECONDS),
                "anchor_id": anchor.anchor_id,
                "decision_iso": _now_iso(),
            }
        )
        self.store.write_pending_timeouts(pending)

    def _drop_pending(self, decision_id: str) -> None:
        pending = self.store.read_pending_timeouts()
        kept = [p for p in pending if p.get("decision_id") != decision_id]
        if len(kept) != len(pending):
            self.store.write_pending_timeouts(kept)

    # ----------------------------------------------------------- timeouts

    def _resolve_render_timeout(self, entry: dict[str, Any]) -> None:
        decision_id = str(entry.get("decision_id") or "")
        if not decision_id:
            return
        self.record_feedback(
            decision_id=decision_id,
            raw_action="timeout",
            metadata={"source": "render_timeout_monitor"},
        )

    def _resolve_null_outcome(self, entry: dict[str, Any]) -> None:
        decision_id = str(entry.get("decision_id") or "")
        if not decision_id:
            return
        bundle = self._decision_cache.get(decision_id)
        track_key = ""
        if bundle is not None:
            obs: ProactiveObservation = bundle["observation"]
            track_key = obs.document_key
        track = self._get_track(track_key)
        vol, churn, idle = track.snapshot_volume()
        outcome = classify_null_outcome(
            edit_volume_since=vol,
            churn_since=churn,
            idle_since=idle,
        )
        if not outcome:
            outcome = "unknown"
        self.store.log_null_outcome(
            {
                "decision_id": decision_id,
                "timestamp": _now_iso(),
                "outcome": outcome,
                "edit_volume_since": vol,
                "churn_since": churn,
                "idle_since": idle,
            }
        )
        self._telemetry.null_outcome(
            decision_id=decision_id,
            outcome=outcome,
            edit_volume=vol,
            churn=churn,
            idle=idle,
        )

    # ----------------------------------------------------------- logging

    def _log_decision_task(
        self,
        *,
        decision_id: str,
        task: ProactiveTask,
        anchor: ActiveAnchor,
        primitive: dict[str, Any],
        context_meta: dict[str, Any],
        evaluator_breakdown: Optional[ScoreBreakdown],
        threshold: float,
        candidate_count: int,
    ) -> None:
        self.store.log_decision(
            {
                "decision_id": decision_id,
                "timestamp": _now_iso(),
                "workspace_id": self.workspace_id,
                "surface": anchor.surface,
                "prediction": "task",
                "task_type": task.task_type,
                "anchor_id_hash": anchor.anchor_id,
                "anchor_confidence": anchor.confidence,
                "anchor_source": anchor.source,
                "context_scope": task.context_scope,
                "render_mode": task.render_mode,
                "candidate_count": candidate_count,
                "evaluator_score": task.evaluator_score,
                "evaluator_breakdown": (
                    asdict(evaluator_breakdown)
                    if evaluator_breakdown is not None
                    else None
                ),
                "threshold": threshold,
                "gate_reasons": [],
                "primitive": dict(primitive),
                "context_meta": dict(context_meta),
                "raw_text_saved": False,
            }
        )

    def _log_decision_null(
        self,
        *,
        decision_id: str,
        null: NullPrediction,
        anchor: ActiveAnchor,
        primitive: dict[str, Any],
        threshold: float,
    ) -> None:
        self.store.log_decision(
            {
                "decision_id": decision_id,
                "timestamp": _now_iso(),
                "workspace_id": self.workspace_id,
                "surface": anchor.surface,
                "prediction": "null",
                "task_type": None,
                "anchor_id_hash": anchor.anchor_id,
                "anchor_confidence": anchor.confidence,
                "anchor_source": anchor.source,
                "reason": null.reason,
                "gate_reasons": list(null.gate_reasons),
                "candidate_count": null.candidate_count,
                "evaluator_score": null.evaluator_score,
                "threshold": threshold,
                "primitive": dict(primitive),
                "raw_text_saved": False,
            }
        )

    # ----------------------------------------------------------- admin

    def explain(self, decision_id: str) -> Optional[dict[str, Any]]:
        bundle = self._decision_cache.get(decision_id)
        if bundle is None:
            return None
        prediction = bundle["prediction"]
        anchor: ActiveAnchor = bundle["anchor"]
        breakdown: Optional[ScoreBreakdown] = bundle.get("evaluator_breakdown")
        out: dict[str, Any] = {
            "decisionId": decision_id,
            "createdAt": bundle.get("created_at"),
            "surface": anchor.surface,
            "anchor": {
                "anchor_id": anchor.anchor_id,
                "confidence": anchor.confidence,
                "source": anchor.source,
                "has_paragraph": bool(anchor.paragraph_text),
                "has_section": bool(anchor.section_heading),
            },
            "candidate_count": bundle.get("candidate_count"),
            "threshold": bundle.get("threshold"),
            "primitive": dict(bundle.get("primitive") or {}),
        }
        if is_task(prediction):
            task: ProactiveTask = prediction  # type: ignore[assignment]
            ctx: Optional[ContextBundle] = bundle.get("context")
            out["prediction"] = "task"
            out["task"] = {
                "task_type": task.task_type,
                "context_scope": task.context_scope,
                "render_mode": task.render_mode,
                "reason": task.reason,
                "evaluator_score": task.evaluator_score,
            }
            if breakdown is not None:
                out["evaluator_breakdown"] = asdict(breakdown)
            if ctx is not None:
                out["context"] = ctx.meta_dict()
        else:
            null: NullPrediction = prediction  # type: ignore[assignment]
            out["prediction"] = "null"
            out["null"] = {
                "reason": null.reason,
                "gate_reasons": list(null.gate_reasons),
                "evaluator_score": null.evaluator_score,
            }
        return out

    def snapshot(self) -> dict[str, Any]:
        return self.store.snapshot()

    def reset(self) -> dict[str, Any]:
        self._decision_cache.clear()
        self._decision_order.clear()
        with self._tracks_lock:
            self._tracks.clear()
        result = self.store.reset()
        self._telemetry.custom("admin", f"adaptation reset workspace={self.workspace_id}")
        return result

    # ----------------------------------------------------------- env

    def _workspace_has_sources(self) -> bool:
        ws_dir = (
            self.output_root / self.workspace_id
            if self.workspace_id != "default"
            else self.output_root / "api"
        )
        try:
            chroma = ws_dir / "chromadb"
            if chroma.exists() and any(chroma.iterdir()):
                return True
            summary = ws_dir / "summary" / "index.json"
            return summary.exists() and summary.stat().st_size > 0
        except OSError:
            return False


__all__ = ["ProactiveOrchestrator"]
