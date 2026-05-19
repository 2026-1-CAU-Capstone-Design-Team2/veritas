"""시나리오 패키지 — 모든 클래스/타입을 외부에 단일 진입점으로 노출.

`from services.screen_tool_funcs.scenario import IdleAfterWritingScenario, ...` 형태로 사용.
실제 구현은 카테고리별 모듈에 분산:
- base.py: ScenarioType, ScenarioContext, ScenarioEvaluation
- _shared.py: 정규식·사전·이벤트 헬퍼 (모듈상수)
- writing_flow.py: 작성 흐름 (5개, Phase 1-3)
- structure.py: 문서 구조 (5개, Phase 4 Tier 1)
- markers.py: 단순 마커 (3개, Phase 4 Tier 1)
- text_quality.py: 텍스트 품질 (6개, Phase 4 Tier 2-A)
- edit_diff.py: 캡처간 편집 변화 (4개, Phase 4 Tier 2-B)
"""
from __future__ import annotations

from .base import ScenarioContext, ScenarioEvaluation, ScenarioType
from .edit_diff import (
    CopyPasteGrowthScenario,
    LargeDeletionScenario,
    ScatteredEditsScenario,
    UndoCycleDetectedScenario,
)
from .markers import (
    AcronymIntroducedScenario,
    ManyQuestionMarksScenario,
    TodoMarkerPresentScenario,
)
from .structure import (
    CodeBlockPresentScenario,
    HeadingAddedScenario,
    LongParagraphWrittenScenario,
    NumberedListGrowthScenario,
    OutlinePhaseScenario,
)
from .text_quality import (
    CitationMissingScenario,
    FactualClaimMadeScenario,
    QuoteInsertedScenario,
    RepeatedPhraseInParagraphScenario,
    TransitionWordOveruseScenario,
    WeakModifierOveruseScenario,
)
from .writing_flow import (
    BlankDocumentStartScenario,
    IdleAfterWritingScenario,
    LongStaticReviewScenario,
    ParagraphChurnScenario,
    WholeDocumentReviewScenario,
)

__all__ = [
    # 베이스 타입
    "ScenarioContext",
    "ScenarioEvaluation",
    "ScenarioType",
    # writing_flow (Phase 1-3, 5)
    "BlankDocumentStartScenario",
    "IdleAfterWritingScenario",
    "LongStaticReviewScenario",
    "ParagraphChurnScenario",
    "WholeDocumentReviewScenario",
    # structure (Phase 4 Tier 1, 5)
    "CodeBlockPresentScenario",
    "HeadingAddedScenario",
    "LongParagraphWrittenScenario",
    "NumberedListGrowthScenario",
    "OutlinePhaseScenario",
    # markers (Phase 4 Tier 1, 3)
    "AcronymIntroducedScenario",
    "ManyQuestionMarksScenario",
    "TodoMarkerPresentScenario",
    # text_quality (Phase 4 Tier 2-A, 6)
    "CitationMissingScenario",
    "FactualClaimMadeScenario",
    "QuoteInsertedScenario",
    "RepeatedPhraseInParagraphScenario",
    "TransitionWordOveruseScenario",
    "WeakModifierOveruseScenario",
    # edit_diff (Phase 4 Tier 2-B, 4)
    "CopyPasteGrowthScenario",
    "LargeDeletionScenario",
    "ScatteredEditsScenario",
    "UndoCycleDetectedScenario",
]
