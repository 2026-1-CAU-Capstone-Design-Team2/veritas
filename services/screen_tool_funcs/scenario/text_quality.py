"""텍스트 품질 시나리오 — 인용/근거/반복/접속어/약한 강조어 등 글의 질 트리거.

포함 시나리오 (Phase 4 Tier 2-A, 6개):
- QuoteInsertedScenario: 큰따옴표 안 20자+ 인용 → 출처/귀속 제안
- CitationMissingScenario: 통계/년도 있는데 인용 마커 부재 → 근거 보강
- FactualClaimMadeScenario: 통계/년도 등 사실 주장 → 검증 도움
- RepeatedPhraseInParagraphScenario: 한 문단 안 같은 ngram 반복 → 표현 다양화
- TransitionWordOveruseScenario: 접속어(`그러나/하지만/또한` 등) 다수 → 흐름 다듬기
- WeakModifierOveruseScenario: 약한 강조어(`매우/정말/아주` 등) 다수 → 구체 표현으로

모두 정규식 또는 닫힌 한국어 사전 매칭 기반.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from ..core.models import FilteredScreenContext
from ._shared import (
    KO_TRANSITION_WORDS,
    KO_WEAK_MODIFIERS,
    _CITATION_MARKER_RE,
    _QUOTE_RE,
    _STATISTIC_RE,
)
from .base import ScenarioContext, ScenarioEvaluation, ScenarioType


class QuoteInsertedScenario(ScenarioType):
    """본문에 큰따옴표 인용이 등장 → 출처/귀속 제안."""

    name = "quote_inserted"
    priority = "medium"

    def __init__(
        self,
        *,
        min_quote_chars: int = 20,
        cooldown_min_seconds: float = 150.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_quote_chars = min_quote_chars
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        quote = self._quote_status(context.filtered)
        passed = bool(quote.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "quote_present": self._gate_result(
                passed, "quote_present" if passed else "no_quote", quote,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("quote_present")
        else:
            evaluation.blockers.append("no_quote")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"quote_status": quote, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "attribution_check", "preferred_action": "suggest_attribution"}

    def _quote_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = filtered.active_editor_text or ""
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "quotes": 0}
        matches = _QUOTE_RE.findall(text)
        passed = bool(matches)
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_quote",
            "quotes": len(matches),
            "min_quote_chars": self.min_quote_chars,
        }


class CitationMissingScenario(ScenarioType):
    """통계/년도 등 사실 주장이 있는데 인용 마커가 부재 → 근거 보강 제안."""

    name = "citation_missing"
    priority = "medium"

    def __init__(
        self,
        *,
        min_statistics: int = 2,
        cooldown_min_seconds: float = 150.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_statistics = min_statistics
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        cite = self._citation_status(context.filtered)
        passed = bool(cite.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "citation_missing": self._gate_result(
                passed, "citation_missing_observed" if passed else "no_citation_gap", cite,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("citation_missing_observed")
        else:
            evaluation.blockers.append("no_citation_gap")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"citation_status": cite, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "full_document"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "evidence_check", "preferred_action": "request_citation"}

    def _citation_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = filtered.active_editor_text or ""
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "statistics": 0, "citations": 0}
        stat_count = len(_STATISTIC_RE.findall(text))
        cite_count = len(_CITATION_MARKER_RE.findall(text))
        passed = stat_count >= self.min_statistics and cite_count == 0
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_citation_gap",
            "statistics": stat_count,
            "citations": cite_count,
            "min_statistics": self.min_statistics,
        }


class FactualClaimMadeScenario(ScenarioType):
    """본문에 통계/년도 등 사실 주장이 등장 → 검증 도와줌."""

    name = "factual_claim_made"
    priority = "medium"

    def __init__(
        self,
        *,
        min_statistics: int = 1,
        cooldown_min_seconds: float = 120.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_statistics = min_statistics
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        claim = self._claim_status(context.filtered)
        passed = bool(claim.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "factual_claim": self._gate_result(
                passed, "factual_claim_observed" if passed else "no_factual_claim", claim,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("factual_claim_observed")
        else:
            evaluation.blockers.append("no_factual_claim")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"claim_status": claim, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "verify", "preferred_action": "verify_claim"}

    def _claim_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = (filtered.current_paragraph_text or filtered.active_editor_text or "")
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "statistics": 0}
        stat_count = len(_STATISTIC_RE.findall(text))
        passed = stat_count >= self.min_statistics
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_factual_claim",
            "statistics": stat_count,
            "min_statistics": self.min_statistics,
        }


class RepeatedPhraseInParagraphScenario(ScenarioType):
    """현재 문단에 같은 ngram(2-단어)이 다수 반복 → 표현 다양화 제안."""

    name = "repeated_phrase_in_paragraph"
    priority = "medium"

    def __init__(
        self,
        *,
        ngram: int = 2,
        min_repeats: int = 3,
        min_paragraph_words: int = 20,
        cooldown_min_seconds: float = 90.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.ngram = ngram
        self.min_repeats = min_repeats
        self.min_paragraph_words = min_paragraph_words
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        repeat = self._repeat_status(context.filtered)
        passed = bool(repeat.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "repeated_phrase": self._gate_result(
                passed, "repeated_phrase_observed" if passed else "no_repeated_phrase", repeat,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("repeated_phrase_observed")
        else:
            evaluation.blockers.append("no_repeated_phrase")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"repeat_status": repeat, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "rephrase", "preferred_action": "suggest_alternative_wording"}

    def _repeat_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = filtered.current_paragraph_text or ""
        words = text.split()
        if len(words) < self.min_paragraph_words:
            return {
                "passed": False,
                "reason": "paragraph_too_short",
                "words": len(words),
                "min_paragraph_words": self.min_paragraph_words,
            }
        if len(words) < self.ngram:
            return {"passed": False, "reason": "ngram_unavailable", "top_ngram": None, "top_count": 0}
        ngrams = [" ".join(words[i : i + self.ngram]) for i in range(len(words) - self.ngram + 1)]
        counts = Counter(ngrams)
        top_ngram, top_count = counts.most_common(1)[0] if counts else (None, 0)
        passed = top_count >= self.min_repeats
        return {
            "passed": passed,
            "reason": "ok" if passed else "no_repeated_phrase",
            "top_ngram": top_ngram,
            "top_count": top_count,
            "min_repeats": self.min_repeats,
            "ngram": self.ngram,
        }


class TransitionWordOveruseScenario(ScenarioType):
    """접속어가 짧은 구간에 다수 → 흐름 다듬기 제안."""

    name = "transition_word_overuse"
    priority = "medium"

    def __init__(
        self,
        *,
        min_count: int = 4,
        cooldown_min_seconds: float = 150.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_count = min_count
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        transition = self._transition_status(context.filtered)
        passed = bool(transition.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "transition_overuse": self._gate_result(
                passed, "transition_overuse_observed" if passed else "transitions_ok", transition,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("transition_overuse_observed")
        else:
            evaluation.blockers.append("transitions_ok")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"transition_status": transition, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "smooth_flow", "preferred_action": "reduce_transitions"}

    def _transition_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = (filtered.current_paragraph_text or filtered.active_editor_text or "")
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "count": 0}
        count = sum(text.count(word) for word in KO_TRANSITION_WORDS)
        passed = count >= self.min_count
        return {
            "passed": passed,
            "reason": "ok" if passed else "transitions_ok",
            "count": count,
            "min_count": self.min_count,
        }


class WeakModifierOveruseScenario(ScenarioType):
    """약한 강조어가 다수 → 구체 표현으로 다듬기 제안."""

    name = "weak_modifier_overuse"
    priority = "medium"

    def __init__(
        self,
        *,
        min_count: int = 4,
        cooldown_min_seconds: float = 150.0,
        initial_vruntime: float | None = None,
        vruntime_increment: float | None = None,
    ) -> None:
        super().__init__(initial_vruntime=initial_vruntime, vruntime_increment=vruntime_increment)
        self.min_count = min_count
        self.cooldown_min_seconds = cooldown_min_seconds

    def evaluate(self, context: ScenarioContext) -> ScenarioEvaluation:
        evaluation = ScenarioEvaluation(name=self.name, priority=self.priority)
        modifier = self._modifier_status(context.filtered)
        passed = bool(modifier.get("passed"))
        cd = self._time_cooldown_status(context.last_fired_at)
        cd_passed = bool(cd.get("passed"))
        evaluation.gate_results = {
            "weak_modifier_overuse": self._gate_result(
                passed, "weak_modifier_overuse_observed" if passed else "modifiers_ok", modifier,
            ),
            "time_cooldown": self._gate_result(
                cd_passed, "time_cooldown_passed" if cd_passed else "time_cooldown_active", cd,
            ),
        }
        if passed:
            evaluation.score += 0.5
            evaluation.reasons.append("weak_modifier_overuse_observed")
        else:
            evaluation.blockers.append("modifiers_ok")
        if cd_passed:
            evaluation.reasons.append("time_cooldown_passed")
        else:
            evaluation.blockers.append("time_cooldown_active")
        evaluation.ready = not evaluation.blockers
        evaluation.priority = "medium" if evaluation.ready else "low"
        evaluation.metadata = {"modifier_status": modifier, "time_cooldown": cd}
        return evaluation

    def writing_context_overrides(self, *, filtered, base):
        return {"focus_scope": "recent_writing"}

    def tool_routing_hint_overrides(self, *, event, base, focused_sentence):
        return {"tone": "tighten", "preferred_action": "concretize_modifiers"}

    def _modifier_status(self, filtered: FilteredScreenContext) -> dict[str, Any]:
        text = (filtered.current_paragraph_text or filtered.active_editor_text or "")
        if not text.strip():
            return {"passed": False, "reason": "empty_text", "count": 0}
        count = sum(text.count(word) for word in KO_WEAK_MODIFIERS)
        passed = count >= self.min_count
        return {
            "passed": passed,
            "reason": "ok" if passed else "modifiers_ok",
            "count": count,
            "min_count": self.min_count,
        }


