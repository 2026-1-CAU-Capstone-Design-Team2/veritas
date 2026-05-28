"""ProactiveOrchestrator — the one entry point for both writing surfaces.

Lifecycle (per workspace bind):

1. ``AgentRuntime._configure_workspace_runtime`` constructs an orchestrator
   and gives it the workspace's output dir + the runtime's ``ChatAgent`` (for
   the generator's downstream LLM calls).
2. The API's ``proactive_service`` calls ``observe`` for each native-cursor /
   external-capture tick and ``record_feedback`` when a button is clicked.
3. The timeout monitor calls back into ``_resolve_render_timeout`` /
   ``_resolve_noop_outcome`` for any pending entry that crosses its horizon.

Per-decision invariants:
- ``observation.text`` lives only in an in-memory cache keyed by
  ``decision_id``; it is *never* written to ``policy_state.json``.
- The decision recorded in ``decisions.jsonl`` carries the feature vectors and
  primitive dict so a later audit can reconstruct what the policy saw.
- Feedback updates the engage policy with ``(I_t, π_t, x_engage, r)`` and the
  suggestion policy with ``(suggestion_type, x_suggest, r_suggest)``.

This file has *no* HTTP/SSE coupling — it returns plain dataclasses /
generators. The API adapter in ``api/services/proactive_service.py`` is the
only place that touches Pydantic / SSE.
"""
from __future__ import annotations

import logging
import random as _random
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .action_space import build_suggestion_action_mask
from .context_selector import SelectedContext, select_context
from .features import (
    ENGAGE_FEATURE_NAMES,
    SUGGEST_FEATURE_NAMES,
    build_engage_features,
    build_suggest_features,
    extract_primitive_features,
)
from .generator import ProactiveGenerator
from .models import (
    FeatureSnapshot,
    FeedbackRecord,
    ProactiveDecision,
    ProactiveObservation,
)
from .policy_store import PolicyStore
from .reward import (
    CANONICAL_REWARD,
    canonicalize_feedback,
    reward_for,
)
from .telemetry import get_telemetry, release_telemetry
from .timeout_monitor import (
    NOOP_OUTCOME_HORIZON_SECONDS,
    TimeoutMonitor,
    classify_noop_outcome,
    now_plus,
    render_timeout_seconds,
)

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


def get_render_mode(surface: str, suggestion_type: str) -> str:
    """Surface + suggestion → render mode (§7.1)."""
    if surface == "external_screen":
        return "external_card"
    if surface == "native_editor" and suggestion_type == "next_sentence":
        return "native_ghost"
    if surface == "native_editor":
        return "native_inline_diff"
    return "none"


class _DocumentTrack:
    """Per-document_key rolling telemetry for one workspace.

    The bandit's primitive features need short-horizon "what changed since
    last observation" signal that the observation itself doesn't carry —
    idle_sec, stable_capture_count, edit_volume, etc. We compute it here from
    the sequence of observe() calls.

    Each instance is light: a recent-observation deque + a few scalars. Kept
    per ``(workspace_id, document_key)`` so two open documents don't pollute
    each other's idle clocks.
    """

    def __init__(self, *, window_seconds: float = 30.0, history_max: int = 32) -> None:
        self._lock = threading.Lock()
        self._history: deque[tuple[float, str]] = deque(maxlen=history_max)  # (ts, text)
        self._last_mutation_ts: float = time.monotonic()
        self._last_intervention_ts: float | None = None
        self._last_text: str = ""
        self._stable_count: int = 0
        self.window_seconds: float = float(window_seconds)

    def observe(self, *, text: str, captured_ts: float) -> dict[str, float | int]:
        """Append a new snapshot and return the derived primitives."""
        with self._lock:
            added = max(0, len(text) - len(self._last_text))
            deleted = max(0, len(self._last_text) - len(text))
            mutation = (text != self._last_text)

            if mutation:
                self._last_mutation_ts = captured_ts
                self._stable_count = 0
            else:
                self._stable_count += 1

            # Drop history older than the rolling window.
            cutoff = captured_ts - self.window_seconds
            while self._history and self._history[0][0] < cutoff:
                self._history.popleft()
            self._history.append((captured_ts, text))

            added_window = added
            deleted_window = deleted
            # Aggregate over the window by diffing consecutive snapshots.
            if len(self._history) >= 2:
                added_window = 0
                deleted_window = 0
                prev = self._history[0][1]
                for _ts, cur in list(self._history)[1:]:
                    a = max(0, len(cur) - len(prev))
                    d = max(0, len(prev) - len(cur))
                    added_window += a
                    deleted_window += d
                    prev = cur

            idle_sec = max(0.0, captured_ts - self._last_mutation_ts)
            time_since_last = (
                (captured_ts - self._last_intervention_ts)
                if self._last_intervention_ts is not None
                else 9999.0
            )
            self._last_text = text
            return {
                "idle_sec": float(idle_sec),
                "stable_capture_count": int(self._stable_count),
                "added_chars_window": float(added_window),
                "deleted_chars_window": float(deleted_window),
                "time_since_last_intervention": float(time_since_last),
            }

    def mark_intervention(self, ts: float | None = None) -> None:
        with self._lock:
            self._last_intervention_ts = float(ts if ts is not None else time.monotonic())

    def snapshot_volume(self) -> tuple[float, float, float]:
        """Used by the no-op outcome classifier — returns (edit_volume,
        churn_score, idle_sec) since the last observation."""
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


class ProactiveOrchestrator:
    """One-per-workspace orchestrator. Constructed lazily by the runtime."""

    def __init__(
        self,
        *,
        output_root: Path,
        workspace_id: str,
        generator: ProactiveGenerator,
        rng: _random.Random | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.workspace_id = workspace_id
        self.store = PolicyStore(output_root=self.output_root, workspace_id=workspace_id)
        self.generator = generator
        self._rng = rng or _random.Random()
        # Telemetry: per-decision/feedback line on console (when --console-logs)
        # AND a per-workspace timeline file under proactive_policy/proactive.log.
        self._telemetry = get_telemetry(
            workspace_id=workspace_id,
            log_dir=self.store.policy_dir,
        )

        # Decision cache: short-lived (ProactiveDecision plus the live
        # observation text the generator needs). Bounded to avoid memory
        # growth; old entries are evicted FIFO.
        self._decision_cache: dict[str, dict[str, Any]] = {}
        self._decision_order: deque[str] = deque(maxlen=512)

        self._tracks: dict[str, _DocumentTrack] = {}
        self._tracks_lock = threading.Lock()

        self._timeout_monitor = TimeoutMonitor(
            on_render_timeout=self._resolve_render_timeout,
            on_noop_outcome=self._resolve_noop_outcome,
            get_pending=self.store.read_pending_timeouts,
            set_pending=self.store.write_pending_timeouts,
        )
        self._timeout_monitor.start()

    # ----------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Stop background threads. Called when the runtime swaps workspace."""
        try:
            self._timeout_monitor.stop()
        except Exception:
            pass
        try:
            self.store.save()
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

    def _stash_decision(
        self,
        *,
        decision_id: str,
        decision: ProactiveDecision,
        observation: ProactiveObservation,
        selected_context: SelectedContext | None,
        x_engage: list[float],
        x_suggest: list[float],
    ) -> None:
        if len(self._decision_order) == self._decision_order.maxlen:
            old = self._decision_order[0]
            self._decision_cache.pop(old, None)
        self._decision_order.append(decision_id)
        self._decision_cache[decision_id] = {
            "decision": decision,
            "observation": observation,
            "selected_context": selected_context,
            "x_engage": x_engage,
            "x_suggest": x_suggest,
            "stored_at": time.monotonic(),
        }

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        """Return the cached decision bundle (for the generator)."""
        return self._decision_cache.get(decision_id)

    def observe(self, observation: ProactiveObservation) -> ProactiveDecision:
        """Run the §14 pipeline: extract → pick candidate → engage decision."""
        captured_ts = time.monotonic()
        track = self._get_track(observation.document_key)
        rolling = track.observe(text=observation.text, captured_ts=captured_ts)

        stats = self.store.get_user_stats()
        primitive = extract_primitive_features(
            observation=observation,
            idle_sec=float(rolling["idle_sec"]),
            stable_capture_count=int(rolling["stable_capture_count"]),
            added_chars_window=float(rolling["added_chars_window"]),
            deleted_chars_window=float(rolling["deleted_chars_window"]),
            recent_negative_rate=float(stats.get("recent_negative_rate", 0.0)),
            time_since_last_intervention=float(rolling["time_since_last_intervention"]),
            relevant_sources_available=self._workspace_has_sources(),
        )

        x_engage = build_engage_features(primitive)
        x_suggest = build_suggest_features(primitive)
        # Pass the surface explicitly — the mask narrows native to
        # next_sentence (paste-ready continuation only), see action_space.py.
        mask = build_suggestion_action_mask(
            primitive, surface_is_native=(observation.surface == "native_editor")
        )

        suggest_pick = self.store.suggestion_policy.select(
            x_suggest, available_actions=mask
        )
        candidate_type: str | None = suggest_pick.get("selected")

        engage_pick = self.store.engage_policy.select(
            x_engage,
            candidate_suggestion_type=candidate_type,
            safety_allowed=True,
            time_since_last_intervention=float(primitive["time_since_last_intervention"]),
            recent_negative_rate=float(primitive["recent_negative_rate"]),
            idle_sec=float(primitive["idle_sec"]),
            rng=self._rng,
        )
        engage_action = engage_pick["selected"]
        pi_t = float(engage_pick["pi_t"])

        decision_id = f"pd_{uuid.uuid4().hex[:16]}"
        created_at = _now_iso()
        feature_snapshot = FeatureSnapshot(
            engage_features=list(x_engage),
            engage_feature_names=list(ENGAGE_FEATURE_NAMES),
            suggest_features=list(x_suggest),
            suggest_feature_names=list(SUGGEST_FEATURE_NAMES),
            primitive=primitive,
        )

        selected_ctx: SelectedContext | None = None
        render_mode = "none"
        suggestion_type: str | None = None
        context_scope: str = "none"
        expires_at = ""

        if engage_action == "intervene" and candidate_type is not None:
            suggestion_type = candidate_type
            selected_ctx = select_context(
                observation=observation,
                suggestion_type=suggestion_type,
                primitive=primitive,
            )
            context_scope = selected_ctx.scope
            render_mode = get_render_mode(observation.surface, suggestion_type)
            expires_at = now_plus(render_timeout_seconds(render_mode))
            track.mark_intervention(captured_ts)
            self.store.mark_intervention(when_iso=created_at)

        decision = ProactiveDecision(
            decision_id=decision_id,
            surface=observation.surface,
            workspace_id=observation.workspace_id,
            document_key=observation.document_key,
            candidate_suggestion_type=candidate_type,
            available_suggestion_actions=list(mask),
            engage_action=engage_action,
            should_intervene=(engage_action == "intervene"),
            intervention_probability=pi_t,
            suggestion_type=suggestion_type,
            context_scope=context_scope,  # type: ignore[arg-type]
            render_mode=render_mode,  # type: ignore[arg-type]
            selected_context=(selected_ctx.to_dict() if selected_ctx else {}),
            feature_snapshot=feature_snapshot,
            policy_info={
                "engage": {
                    "p_positive": engage_pick.get("p_positive"),
                    "mean": engage_pick.get("mean"),
                    "std": engage_pick.get("std"),
                    "gate_reason": engage_pick.get("gate_reason"),
                    "roll": engage_pick.get("roll"),
                    "warmup_active": engage_pick.get("warmup_active"),
                    "warmup_remaining": engage_pick.get("warmup_remaining"),
                    "warmup_floor_used": engage_pick.get("warmup_floor_used"),
                    "total_decisions": engage_pick.get("total_decisions"),
                },
                "suggest": {
                    "scores": suggest_pick.get("scores"),
                    "available": suggest_pick.get("available"),
                },
            },
            created_at=created_at,
            expires_at=expires_at,
        )

        self._stash_decision(
            decision_id=decision_id,
            decision=decision,
            observation=observation,
            selected_context=selected_ctx,
            x_engage=x_engage,
            x_suggest=x_suggest,
        )
        self._log_decision(decision)
        self._register_pending(decision, observation, primitive)
        self._telemetry.decision(
            decision_id=decision.decision_id,
            surface=decision.surface,
            engage_action=decision.engage_action,
            intervention_probability=decision.intervention_probability,
            candidate=decision.candidate_suggestion_type,
            suggestion_type=decision.suggestion_type,
            render_mode=decision.render_mode,
            engage_info=dict((decision.policy_info or {}).get("engage", {}) or {}),
            primitive=dict(primitive or {}),
            available=list(decision.available_suggestion_actions or []),
            suggest_scores=dict(suggest_pick.get("scores") or {}),
        )
        return decision

    # ----------------------------------------------------------- generate

    def stream_generation(self, decision_id: str) -> Iterator[dict[str, Any]]:
        """Yield SSE-style ``{type, ...}`` events from the generator.

        Returns an empty iterator if the decision is unknown or was a no-op
        (the caller is expected to filter on ``should_intervene`` before
        calling).
        """
        bundle = self._decision_cache.get(decision_id)
        if bundle is None:
            return iter(())
        decision: ProactiveDecision = bundle["decision"]
        if not decision.should_intervene or decision.suggestion_type is None:
            return iter(())
        selected_ctx: SelectedContext | None = bundle.get("selected_context")
        observation: ProactiveObservation = bundle["observation"]
        return self.generator.stream(
            decision=decision,
            observation=observation,
            selected_context=selected_ctx,
        )

    # ----------------------------------------------------------- feedback

    def record_feedback(
        self,
        *,
        decision_id: str,
        raw_action: str,
        metadata: dict[str, Any] | None = None,
    ) -> FeedbackRecord:
        bundle = self._decision_cache.get(decision_id)
        if bundle is None:
            # No cached bundle — we still record the feedback, but we can't
            # update the policies because we don't have the features. This is
            # how cancellations / late timeouts behave when the cache rolled.
            canonical = canonicalize_feedback(surface="external_screen", raw_action=raw_action)
            record = FeedbackRecord(
                decision_id=decision_id,
                surface="external_screen",
                feedback_action=canonical,  # type: ignore[arg-type]
                engage_reward=None,
                suggestion_reward=None,
                recorded_at=_now_iso(),
                metadata={"reason": "decision_not_in_cache", **(metadata or {})},
            )
            self.store.log_feedback(asdict(record))
            return record

        decision: ProactiveDecision = bundle["decision"]
        canonical = canonicalize_feedback(
            surface=decision.surface, raw_action=raw_action
        )
        engage_r, suggest_r = reward_for(canonical)

        record = FeedbackRecord(
            decision_id=decision_id,
            surface=decision.surface,
            feedback_action=canonical,  # type: ignore[arg-type]
            engage_reward=engage_r,
            suggestion_reward=suggest_r,
            recorded_at=_now_iso(),
            metadata=dict(metadata or {}),
        )

        self._apply_updates(bundle=bundle, canonical=canonical, engage_r=engage_r, suggest_r=suggest_r)
        self.store.apply_feedback_to_stats(
            canonical=canonical,
            intervention_recorded_at=(
                decision.created_at if decision.should_intervene else None
            ),
        )
        self.store.log_feedback(asdict(record))
        self.store.save()
        # Resolve any pending timeout entry for this decision.
        self._drop_pending(decision_id)
        self._telemetry.feedback(
            decision_id=decision.decision_id,
            surface=decision.surface,
            canonical=canonical,
            engage_reward=engage_r,
            suggestion_reward=suggest_r,
            decision_created_at=decision.created_at,
        )
        return record

    def _apply_updates(
        self,
        *,
        bundle: dict[str, Any],
        canonical: str,
        engage_r: float | None,
        suggest_r: float | None,
    ) -> None:
        decision: ProactiveDecision = bundle["decision"]
        x_engage: list[float] = bundle["x_engage"]
        x_suggest: list[float] = bundle["x_suggest"]

        engage_update_info: dict[str, Any] = {}
        suggest_update_info: dict[str, Any] = {}

        if engage_r is not None:
            engage_update_info = self.store.engage_policy.update(
                x=x_engage,
                engage_action=decision.engage_action,
                pi_t=decision.intervention_probability,
                reward=engage_r,
            )

        if (
            decision.should_intervene
            and decision.suggestion_type
            and suggest_r is not None
        ):
            self.store.suggestion_policy.update(
                action=decision.suggestion_type,
                x=x_suggest,
                reward=suggest_r,
            )
            suggest_update_info = {
                "updated": True,
                "action": decision.suggestion_type,
                "reward": suggest_r,
            }

        self.store.log_update(
            {
                "decision_id": decision.decision_id,
                "canonical": canonical,
                "engage_update": engage_update_info,
                "suggest_update": suggest_update_info,
                "applied_at": _now_iso(),
            }
        )
        self._telemetry.update(
            decision_id=decision.decision_id,
            engage_update=engage_update_info,
            suggest_update=suggest_update_info,
        )

    # ----------------------------------------------------------- pending

    def _register_pending(
        self,
        decision: ProactiveDecision,
        observation: ProactiveObservation,
        primitive: dict[str, Any],
    ) -> None:
        pending = self.store.read_pending_timeouts()
        if decision.should_intervene:
            pending.append(
                {
                    "kind": "render_timeout",
                    "decision_id": decision.decision_id,
                    "expires_at": decision.expires_at,
                    "render_mode": decision.render_mode,
                    "workspace_id": decision.workspace_id,
                    "document_key": decision.document_key,
                }
            )
        else:
            pending.append(
                {
                    "kind": "noop_outcome",
                    "decision_id": decision.decision_id,
                    "expires_at": now_plus(NOOP_OUTCOME_HORIZON_SECONDS),
                    "workspace_id": decision.workspace_id,
                    "document_key": decision.document_key,
                    "primitive_at_decision": {
                        "edit_volume": float(primitive.get("edit_volume", 0.0)),
                        "churn_score": float(primitive.get("churn_score", 0.0)),
                        "idle_sec": float(primitive.get("idle_sec", 0.0)),
                    },
                    "decision_iso": decision.created_at,
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
            metadata={"source": "backend_timeout_monitor", "kind": "render_timeout"},
        )

    def _resolve_noop_outcome(self, entry: dict[str, Any]) -> None:
        decision_id = str(entry.get("decision_id") or "")
        if not decision_id:
            return
        document_key = str(entry.get("document_key") or "")
        track = self._get_track(document_key)
        vol, churn, idle = track.snapshot_volume()
        decision_iso = str(entry.get("decision_iso") or "")
        outcome = classify_noop_outcome(
            decision_iso=decision_iso,
            edit_volume_since=vol,
            churn_score_since=churn,
            idle_sec_since=idle,
        )
        if not outcome:
            return  # Not enough signal — skip update entirely.
        self._telemetry.noop_outcome(
            decision_id=decision_id,
            outcome=outcome,
            edit_volume=vol,
            churn=churn,
            idle=idle,
        )
        # The synthetic surface "noop_outcome" routes straight onto the
        # canonical bucket without going through native/external aliases.
        self.record_feedback(
            decision_id=decision_id,
            raw_action=outcome,
            metadata={"source": "backend_timeout_monitor", "kind": "noop_outcome"},
        )

    # ----------------------------------------------------------- logging

    def _log_decision(self, decision: ProactiveDecision) -> None:
        snap = decision.feature_snapshot
        meta_ctx = decision.selected_context or {}
        # Strip raw text from the persisted record — keep counts and offsets
        # only. The in-memory cache still has the full text.
        meta_ctx_persist = {
            k: v
            for k, v in meta_ctx.items()
            if k not in {"text", "prefix", "suffix", "original_text", "changed_text", "current_paragraph", "previous_paragraph", "focused_sentence"}
        }
        if "text" in meta_ctx:
            meta_ctx_persist["text_chars"] = len(str(meta_ctx.get("text") or ""))

        self.store.log_decision(
            {
                "decision_id": decision.decision_id,
                "created_at": decision.created_at,
                "expires_at": decision.expires_at,
                "surface": decision.surface,
                "workspace_id": decision.workspace_id,
                "document_key": decision.document_key,
                "candidate_suggestion_type": decision.candidate_suggestion_type,
                "engage_action": decision.engage_action,
                "intervention_probability": decision.intervention_probability,
                "suggestion_type": decision.suggestion_type,
                "context_scope": decision.context_scope,
                "render_mode": decision.render_mode,
                "available_suggestion_actions": list(decision.available_suggestion_actions),
                "engage_features": list(snap.engage_features) if snap else [],
                "suggest_features": list(snap.suggest_features) if snap else [],
                "primitive": dict(snap.primitive) if snap else {},
                "policy_info": dict(decision.policy_info or {}),
                "selected_context_meta": meta_ctx_persist,
            }
        )

    # ----------------------------------------------------------- explain

    def explain(self, decision_id: str) -> dict[str, Any] | None:
        """Human-readable trace of one decision for ``--proactive-debug``
        operators who can't easily parse the JSONL feature vector by eye.

        Returns ``None`` if the decisionId has rolled out of the in-memory
        cache (after ~512 decisions per workspace) — the JSONL log still has
        the same data in raw form.
        """
        bundle = self._decision_cache.get(decision_id)
        if bundle is None:
            return None
        decision: ProactiveDecision = bundle["decision"]
        snap = decision.feature_snapshot
        primitive = dict(snap.primitive) if snap else {}
        engage_info = (decision.policy_info or {}).get("engage") or {}
        suggest_info = (decision.policy_info or {}).get("suggest") or {}
        scores = dict(suggest_info.get("scores") or {})
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:3]
        selected_ctx_meta: dict[str, Any] = {}
        if decision.selected_context:
            text = str(decision.selected_context.get("text") or "")
            selected_ctx_meta = {
                "scope": decision.selected_context.get("scope"),
                "text_chars": len(text),
                "snippet": (text[:160] + "…") if len(text) > 160 else text,
                "target_start": decision.selected_context.get("target_start"),
                "target_end": decision.selected_context.get("target_end"),
                "needs_rag": decision.selected_context.get("needs_rag"),
            }
        return {
            "decisionId": decision.decision_id,
            "surface": decision.surface,
            "createdAt": decision.created_at,
            "engage": {
                "engageAction": decision.engage_action,
                "shouldIntervene": decision.should_intervene,
                "pi_t": decision.intervention_probability,
                "p_positive": engage_info.get("p_positive"),
                "mean": engage_info.get("mean"),
                "std": engage_info.get("std"),
                "gateReason": engage_info.get("gate_reason"),
                "warmupActive": engage_info.get("warmup_active"),
                "warmupRemaining": engage_info.get("warmup_remaining"),
                "totalDecisions": engage_info.get("total_decisions"),
            },
            "suggestion": {
                "chosen": decision.suggestion_type,
                "candidate": decision.candidate_suggestion_type,
                "mask": list(decision.available_suggestion_actions or []),
                "topScores": [
                    {"arm": arm, "ucb": score} for arm, score in ranked
                ],
            },
            "context": {
                "scope": decision.context_scope,
                "renderMode": decision.render_mode,
                **selected_ctx_meta,
            },
            "primitive": primitive,
        }

    # ----------------------------------------------------------- admin

    def snapshot(self) -> dict[str, Any]:
        """Compact "where is the bandit right now?" summary for the operator."""
        snap = self.store.snapshot()
        snap["pending_timeouts"] = self.store.read_pending_timeouts()
        return snap

    def reset(self) -> dict[str, Any]:
        """Drop learned state, keep history. Also clears the in-memory caches
        so a stale decisionId from before the reset can't update the new
        policies."""
        self._decision_cache.clear()
        self._decision_order.clear()
        with self._tracks_lock:
            self._tracks.clear()
        result = self.store.reset()
        # Pending timeouts pointed at decisions that don't exist anymore —
        # wipe them rather than letting the sweeper resolve stale entries.
        self.store.write_pending_timeouts([])
        self._telemetry.custom("admin", f"policy reset workspace={self.workspace_id}")
        return result

    # ----------------------------------------------------------- env

    def _workspace_has_sources(self) -> bool:
        """Cheap "does the workspace have any indexed sources?" probe.

        Per §4.2 of the spec, we MUST NOT do RAG retrieval on the high-
        frequency observe path. So we only check whether the index directory
        exists with non-empty content — this is an O(1) stat, not a query.
        """
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


__all__ = [
    "ProactiveOrchestrator",
    "get_render_mode",
]
