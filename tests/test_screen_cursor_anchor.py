"""Regression tests: 외부 앱 실시간 보조가 (1) 커서 위치(=작성 위치)에 앵커되고,
(2) 카드가 '붙여넣기 가능한 제안 + 회색 설명'으로 분리되는지 가드한다.

배경(사용자 보고):
1. 결론을 작성 중인데도 서론 문장에 대한 검사 결과가 돌아옴 — caret 미검출 시
   마우스 hover/문서 머리로 fallback되던 버그.
2. 복사 가능한 제안과 설명이 분리되지 않음 — 카드의 content/note 분리가 모델의
   "설명:" 누락에 취약했음.
"""
from __future__ import annotations

import os
import unittest

from services.screen_tool_funcs.core.content_filter import (
    ContentFilter,
    resolve_cursor_scope,
)
from services.screen_tool_funcs.core.models import FilteredScreenContext, WindowContext
from services.screen_tool_funcs.intervention.intervention_dispatcher import (
    InterventionDispatcher,
)
from services.screen_tool_funcs.scenario.base import ScenarioContext
from services.screen_tool_funcs.scenario.markers import AcronymIntroducedScenario


INTRO = "서론 문장입니다. 이 보고서는 전기차 시장을 다룬다."
BODY = "본론에서 여러 요인을 분석한다. 수요와 공급을 함께 본다."
CONCL = "결론적으로 규제는 강화될 것으로 보인다. 마지막 문장이다."
FULL = f"{INTRO}\n\n{BODY}\n\n{CONCL}"


class DiffRegionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cf = ContentFilter()

    def test_first_capture_returns_empty(self) -> None:
        # previous 없음 → 문서 전체를 "방금 변경"으로 보지 않는다(서론 발화 방지).
        self.assertEqual(self.cf._diff_region("", FULL), ("", None))

    def test_no_change_returns_empty(self) -> None:
        self.assertEqual(self.cf._diff_region(FULL, FULL), ("", None))

    def test_pure_append_returns_appended_tail_and_offset(self) -> None:
        prev = f"{INTRO}\n\n{BODY}"
        cur = f"{INTRO}\n\n{BODY}\n\n{CONCL}"
        region, off = self.cf._diff_region(prev, cur)
        self.assertIn("결론", region)
        self.assertNotIn("서론", region)
        # 커서 offset = 변경 끝 = 문서 끝(append).
        self.assertEqual(off, len(cur))

    def test_mid_document_edit_returns_bounded_region(self) -> None:
        prev = FULL
        # 본론 중간에 한 문장 삽입 (append 아님).
        cur = FULL.replace("수요와 공급을 함께 본다.", "수요와 공급, 그리고 정책을 함께 본다.")
        region, off = self.cf._diff_region(prev, cur)
        self.assertIn("정책", region)
        self.assertNotIn("서론", region)
        self.assertNotIn("마지막 문장", region)
        # 커서 offset은 삽입 지점 끝 — 그 앞은 "정책"을 포함한다.
        self.assertIn("정책", cur[:off])


class ResolveAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.disp = InterventionDispatcher(None, scenarios={})

    def _filtered(
        self, *, paragraph: str, changed: str, scope: str = ""
    ) -> FilteredScreenContext:
        return FilteredScreenContext(
            active_editor_text=FULL,
            current_paragraph_text=paragraph,
            cursor_scope_text=scope,
            changed_text=changed,
        )

    def test_uses_precomputed_cursor_scope_text(self) -> None:
        # content_filter가 채운 cursor_scope_text를 그대로 anchor로 쓴다(단일 소스).
        f = self._filtered(paragraph=FULL, changed="마지막 문장이다.", scope=CONCL)
        anchor, hint = self.disp._resolve_anchor(f)
        self.assertEqual(anchor, CONCL)
        self.assertEqual(hint, "마지막 문장이다.")

    def test_caret_paragraph_is_primary_anchor_when_scope_empty(self) -> None:
        # cursor_scope_text 비면(구버전) caret 문단으로 재계산.
        f = self._filtered(paragraph=CONCL, changed="마지막 문장이다.")
        anchor, hint = self.disp._resolve_anchor(f)
        self.assertEqual(anchor, CONCL)
        self.assertEqual(hint, "마지막 문장이다.")

    def test_no_caret_no_offset_falls_back_to_tail_not_tiny_diff(self) -> None:
        # caret==전체문서 + offset 없음 → 문서 꼬리(작은 diff "로"가 scope 되지 않음).
        f = self._filtered(paragraph=FULL, changed="로")
        anchor, _hint = self.disp._resolve_anchor(f)
        self.assertNotEqual(anchor, "로")
        self.assertTrue(anchor.endswith("마지막 문장이다."))

    def test_focused_sentence_never_intro_when_writing_conclusion(self) -> None:
        # 핵심 회귀: 결론 작성 중 caret 실패 + 최근 편집은 결론 → focus는 결론.
        for changed in ("마지막 문장이다.", ""):
            f = self._filtered(paragraph=FULL, changed=changed)
            anchor, hint = self.disp._resolve_anchor(f)
            focused = self.disp._focused_sentence(paragraph=anchor, changed_text=hint)
            self.assertNotIn("서론", focused)
            self.assertIn("마지막", focused)

    def test_focused_sentence_anchors_to_edit_tail(self) -> None:
        # 편집영역의 끝(커서)이 있는 문장을 고른다(머리가 아님).
        paragraph = "첫 문장이다. 둘째 문장이다. 셋째 문장이다."
        focused = self.disp._focused_sentence(paragraph=paragraph, changed_text="셋째 문장")
        self.assertIn("셋째", focused)

    def test_recent_sentences_from_caret_paragraph_excludes_intro(self) -> None:
        f = self._filtered(paragraph=CONCL, changed="마지막 문장이다.")
        anchor, _ = self.disp._resolve_anchor(f)
        recent = self.disp._recent_sentences(anchor, limit=2)
        self.assertNotIn("서론", recent)
        self.assertIn("결론", recent)


class ResolveCursorScopeTests(unittest.TestCase):
    """공유 resolve_cursor_scope: 시나리오 스코프와 LLM 앵커의 단일 소스."""

    def test_caret_paragraph_when_real(self) -> None:
        scope, hint = resolve_cursor_scope(full=FULL, caret=CONCL, changed="마지막 문장이다.")
        self.assertEqual(scope, CONCL)
        self.assertEqual(hint, "마지막 문장이다.")

    def test_whole_doc_caret_with_offset_uses_window_not_tiny_diff(self) -> None:
        # 케이스1 회귀(notepad): caret==전체문서(uia_full_text_fallback), 방금 친
        # diff는 한 조각("로"). offset 윈도우로 커서 앞 실제 텍스트를 scope로 — "로"
        # 같은 쓰레기 한 조각이 아니라.
        full = "앞 문단입니다. 결론적으로 규제는 강화될 것입니다. 1.로"
        off = len(full)  # 커서 = 문서 끝
        scope, hint = resolve_cursor_scope(
            full=full, caret=full, changed="로", cursor_offset=off
        )
        self.assertNotEqual(scope, "로")
        self.assertIn("결론", scope)
        self.assertEqual(hint, "로")  # focus hint는 작은 diff 그대로(문장 선택용)

    def test_whole_doc_caret_no_offset_falls_to_tail_not_tiny_diff(self) -> None:
        scope, _ = resolve_cursor_scope(full=FULL, caret=FULL, changed="로")
        self.assertNotEqual(scope, "로")
        self.assertTrue(scope.endswith("마지막 문장이다."))

    def test_offset_window_excludes_intro_in_long_doc(self) -> None:
        long_full = "서론 시작입니다. " + ("본문 문장. " * 200) + "커서앞 문장입니다."
        off = len(long_full)
        scope, _ = resolve_cursor_scope(
            full=long_full, caret=long_full, changed="다", cursor_offset=off
        )
        self.assertIn("커서앞 문장", scope)
        self.assertNotIn("서론 시작", scope)
        self.assertLessEqual(len(scope), 600)

    def test_no_caret_no_change_uses_tail(self) -> None:
        # 짧은 문서면 tail(800자)이 문서 전체와 같다(서론 포함은 정상). 핵심은 꼬리
        # 기준이라 끝이 마지막 문장이라는 것 — 긴 문서에선 서론이 잘려 나간다.
        scope, _ = resolve_cursor_scope(full=FULL, caret="", changed="")
        self.assertTrue(scope.endswith("마지막 문장이다."))
        long_full = ("긴 본문 문장. " * 200) + "맨끝 문장이다."
        long_scope, _ = resolve_cursor_scope(full=long_full, caret="", changed="")
        self.assertTrue(long_scope.endswith("맨끝 문장이다."))
        self.assertLessEqual(len(long_scope), 800)

    def test_content_filter_populates_cursor_scope(self) -> None:
        cf = ContentFilter()
        from services.screen_tool_funcs.core.models import (
            OcrResult,
            UiAutomationResult,
        )

        ui = UiAutomationResult(
            text=FULL,
            current_paragraph_text=CONCL,
            source_quality="usable",
        )
        win = WindowContext(process_name="notepad.exe", window_title="memo.txt")
        filtered = cf.build(window=win, ocr=OcrResult(), ui_automation=ui)
        # caret 문단이 잡혔으면 cursor_scope_text = 그 문단(결론), 서론 아님.
        self.assertIn("결론", filtered.cursor_scope_text)
        self.assertNotIn("서론", filtered.cursor_scope_text)


class ScenarioScopingTests(unittest.TestCase):
    """위치 특정 시나리오는 cursor_scope_text만 보고, 본문 다른 곳 트리거엔
    발화하지 않는다 (이미지 버그: 결론 작성 중 본문 약어에 발화)."""

    def _context(self, *, scope: str, full: str) -> ScenarioContext:
        filtered = FilteredScreenContext(
            active_editor_text=full,
            current_paragraph_text=scope,
            cursor_scope_text=scope,
        )
        return ScenarioContext(
            window=WindowContext(),
            filtered=filtered,
            history_events=[],
            same_document_events=[],
            document_key="doc",
            paragraph_fingerprint="fp",
        )

    def test_acronym_in_body_not_at_cursor_does_not_fire(self) -> None:
        # 커서=결론(약어 없음), 본문 어딘가 "API" 약어 → 발화 안 함.
        full = "본문에 API 라는 약어가 정의 없이 등장한다.\n\n결론적으로 규제는 강화될 것이다."
        ctx = self._context(scope="결론적으로 규제는 강화될 것이다.", full=full)
        ev = AcronymIntroducedScenario().evaluate(ctx)
        self.assertFalse(ev.ready)

    def test_acronym_in_cursor_scope_fires(self) -> None:
        # 커서 영역에 약어가 있으면 발화.
        ctx = self._context(
            scope="여기서 API 를 도입한다.",
            full="앞 문단.\n\n여기서 API 를 도입한다.",
        )
        ev = AcronymIntroducedScenario().evaluate(ctx)
        self.assertTrue(ev.ready)


class CursorLocatedTests(unittest.TestCase):
    """cursor_located: 커서를 신뢰있게 잡았을 때만 True (native editor 방식 게이트)."""

    def setUp(self) -> None:
        self.cf = ContentFilter()
        from services.screen_tool_funcs.core.models import OcrResult, UiAutomationResult

        self._Ocr = OcrResult
        self._Uia = UiAutomationResult
        self._win = WindowContext(process_name="notepad.exe", window_title="memo.txt")

    def test_real_caret_paragraph_is_located(self) -> None:
        ui = self._Uia(
            text=FULL,
            current_paragraph_text=CONCL,
            current_paragraph_source="selection_paragraph",
            source_quality="usable",
        )
        f = self.cf.build(window=self._win, ocr=self._Ocr(), ui_automation=ui)
        self.assertTrue(f.cursor_located)

    def test_full_text_fallback_with_edit_is_located(self) -> None:
        # caret 문단은 못 줬지만(uia_full_text_fallback) 캡처 간 diff가 커서를 드러냄.
        ui = self._Uia(text=FULL, current_paragraph_text="", source_quality="usable")
        f = self.cf.build(
            window=self._win, ocr=self._Ocr(), ui_automation=ui, previous_text=FULL[:-6]
        )
        self.assertEqual(f.current_paragraph_source, "uia_full_text_fallback")
        self.assertTrue(f.cursor_located)

    def test_ocr_source_is_not_located(self) -> None:
        ocr = self._Ocr(text="화면 통째로 읽힌 긴 OCR 텍스트입니다. 코드와 로그가 섞여 있다.")
        ui = self._Uia(error="skipped")
        f = self.cf.build(window=self._win, ocr=ocr, ui_automation=ui)
        self.assertEqual(f.current_paragraph_source, "ocr_same_as_full_text")
        self.assertFalse(f.cursor_located)

    def test_full_text_fallback_no_edit_is_not_located(self) -> None:
        # caret 없음 + 변경 없음(첫 캡처) → 커서 위치 모름 → not located.
        ui = self._Uia(text=FULL, current_paragraph_text="", source_quality="usable")
        f = self.cf.build(window=self._win, ocr=self._Ocr(), ui_automation=ui)
        self.assertFalse(f.cursor_located)

    def test_sticky_offset_keeps_located_through_pause(self) -> None:
        # 핵심 회귀: notepad(caret 미검출)에서 편집 후 멈추면 현재 캡처엔 diff가
        # 없지만, sticky offset으로 located 유지 → idle(이어쓰기)이 발화 가능.
        ui = self._Uia(text=FULL, current_paragraph_text="", source_quality="usable")
        # 편집 캡처(diff 있음) → located.
        f1 = self.cf.build(
            window=self._win, ocr=self._Ocr(), ui_automation=ui, previous_text=FULL[:-6]
        )
        self.assertTrue(f1.cursor_located)
        # 멈춤 캡처(텍스트 동일, diff 없음) → sticky로 여전히 located.
        f2 = self.cf.build(
            window=self._win, ocr=self._Ocr(), ui_automation=ui, previous_text=FULL
        )
        self.assertTrue(f2.cursor_located)
        # cursor_scope도 꼬리 추정이 아니라 커서 앞 윈도우 유지.
        self.assertTrue(f2.cursor_scope_text)

    def test_sticky_does_not_carry_to_different_document(self) -> None:
        ui1 = self._Uia(text=FULL, current_paragraph_text="", source_quality="usable")
        self.cf.build(
            window=self._win, ocr=self._Ocr(), ui_automation=ui1, previous_text=FULL[:-6]
        )
        # 다른 문서(텍스트 다름) + 변경 없음 → sticky 미적용 → not located.
        other = "완전히 다른 문서의 내용입니다. 이전 커서와 무관하다."
        ui2 = self._Uia(text=other, current_paragraph_text="", source_quality="usable")
        f = self.cf.build(
            window=self._win, ocr=self._Ocr(), ui_automation=ui2, previous_text=other
        )
        self.assertFalse(f.cursor_located)


class SectionHeadingTests(unittest.TestCase):
    """커서가 속한 섹션 헤딩을 추출·주입 (native editor 동일)."""

    def setUp(self) -> None:
        self.cf = ContentFilter()
        from services.screen_tool_funcs.core.models import OcrResult, UiAutomationResult

        self._Ocr = OcrResult
        self._Uia = UiAutomationResult
        self._win = WindowContext(process_name="notepad.exe", window_title="memo.txt")

    def test_nearest_heading_picks_last_before_caret(self) -> None:
        text = "# 제목\n\n## 서론\n앞 내용.\n\n## 결론\n따라서 정리하면"
        self.assertEqual(ContentFilter._nearest_heading(text), "결론")

    def test_no_heading_returns_empty(self) -> None:
        self.assertEqual(ContentFilter._nearest_heading("그냥 평문입니다.\n다음 줄."), "")

    def test_build_injects_section_when_writing_under_heading(self) -> None:
        # "## 결론" 아래에서 작성 중(타이핑으로 위치 확정) → section_heading="결론".
        doc = "## 서론\n도입부.\n\n## 결론\n따라서 다음과 같은 결론을 도출"
        ui = self._Uia(text=doc, current_paragraph_text="", source_quality="usable")
        f = self.cf.build(
            window=self._win, ocr=self._Ocr(), ui_automation=ui, previous_text=doc[:-4]
        )
        self.assertTrue(f.cursor_located)
        self.assertEqual(f.section_heading, "결론")

    def test_not_located_has_no_section(self) -> None:
        doc = "## 결론\n따라서 정리하면 다음과 같다."
        ui = self._Uia(text=doc, current_paragraph_text="", source_quality="usable")
        f = self.cf.build(window=self._win, ocr=self._Ocr(), ui_automation=ui)
        self.assertFalse(f.cursor_located)
        self.assertEqual(f.section_heading, "")


class CursorRequiredGateTests(unittest.TestCase):
    """cursor_located=False면 idle/churn은 발화 안 함, blank는 발화 가능."""

    def test_unlocated_drops_idle_and_churn_keeps_blank(self) -> None:
        from services.screen_tool_funcs.intervention.intervention_detector import (
            InterventionDetector,
        )

        ready = ["idle_after_writing", "paragraph_churn", "blank_document_start"]
        kept, dropped = InterventionDetector.filter_unlocated(ready, cursor_located=False)
        self.assertEqual(kept, ["blank_document_start"])
        self.assertCountEqual(dropped, ["idle_after_writing", "paragraph_churn"])

    def test_located_keeps_all(self) -> None:
        from services.screen_tool_funcs.intervention.intervention_detector import (
            InterventionDetector,
        )

        ready = ["idle_after_writing", "paragraph_churn"]
        kept, dropped = InterventionDetector.filter_unlocated(ready, cursor_located=True)
        self.assertEqual(kept, ready)
        self.assertEqual(dropped, [])


class NavJunkFilterTests(unittest.TestCase):
    """KB nav-menu/link-list 청크 필터 (스크랩 boilerplate가 프롬프트 오염)."""

    def setUp(self) -> None:
        from agent.chat_agent import ChatAgent

        self._is_junk = ChatAgent._is_nav_junk

    def test_nav_link_list_is_junk(self) -> None:
        nav = (
            "* [컨퍼런스](https://www.aitimes.kr/news/a?b=1)\n"
            "* [포럼](https://www.aitimes.kr/news/a?b=2)\n"
            "* [발표](https://www.aitimes.kr/news/a?b=3)\n"
            "* [교육](https://www.aitimes.kr/news/a?b=4)\n"
            "* [이벤트](https://www.aitimes.kr/news/a?b=5)"
        )
        self.assertTrue(self._is_junk(nav))

    def test_prose_is_not_junk(self) -> None:
        prose = (
            "AI 에이전트 시장은 2025년 이후 빠르게 성장할 것으로 전망된다. "
            "주요 기업들은 인프라 투자를 늘리고 있으며, 한국 기업의 참여도 확대되고 있다."
        )
        self.assertFalse(self._is_junk(prose))

    def test_empty_is_junk(self) -> None:
        self.assertTrue(self._is_junk(""))

    def test_prose_with_one_link_survives(self) -> None:
        prose = (
            "구글 클라우드가 2026년 AI 에이전트 트렌드 보고서를 공개했다. "
            "자세한 내용은 [원문](https://example.com/report) 에서 확인할 수 있으며, "
            "기업들이 당장 적용할 수 있는 실용 인사이트를 담고 있다."
        )
        self.assertFalse(self._is_junk(prose))


class ScreenAnswerTokenCapTests(unittest.TestCase):
    """screen answer 호출에 max_tokens 명시(잘림 방지)."""

    def test_screen_call_request_sets_max_tokens(self) -> None:
        from agent.chat_agent import ChatAgent

        req = ChatAgent._screen_call_request(
            ChatAgent.__new__(ChatAgent),
            system_prompt="sys",
            prompt="user",
            record_content="q",
            stream_label="screen_context",
        )
        self.assertIsNotNone(req.extra_sampling_params)
        self.assertGreaterEqual(int(req.extra_sampling_params["max_tokens"]), 64)


class OcrSuppressionTests(unittest.TestCase):
    """OCR-only 소스면 위치 특정 prose 시나리오를 ready set에서 제거한다
    (이미지 케이스2: VS Code/콘솔을 OCR → 약어가 OCR 쓰레기 위에 발화)."""

    def test_ocr_source_drops_location_scenarios(self) -> None:
        from services.screen_tool_funcs.intervention.intervention_detector import (
            InterventionDetector,
        )

        ready = ["acronym_introduced", "citation_missing", "whole_document_review"]
        kept, dropped = InterventionDetector.filter_ocr_suppressed(
            ready, "ocr_same_as_full_text"
        )
        self.assertEqual(kept, ["whole_document_review"])
        self.assertIn("acronym_introduced", dropped)
        self.assertIn("citation_missing", dropped)

    def test_non_ocr_source_keeps_all(self) -> None:
        from services.screen_tool_funcs.intervention.intervention_detector import (
            InterventionDetector,
        )

        ready = ["acronym_introduced", "whole_document_review"]
        kept, dropped = InterventionDetector.filter_ocr_suppressed(
            ready, "uia_text_range"
        )
        self.assertEqual(kept, ready)
        self.assertEqual(dropped, [])


# ---------------------------------------------------------------- card split

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication([])
    _QT_OK = True
except Exception:  # pragma: no cover - no Qt / no offscreen platform
    _QT_OK = False


@unittest.skipUnless(_QT_OK, "PySide6 offscreen QApplication unavailable")
class SuggestionCardSplitTests(unittest.TestCase):
    """카드의 content(복사 대상) / note(회색 설명) 분리."""

    def _split(self, text: str):
        from frontend.ui.windows.document_assist_window import SuggestionCard

        return SuggestionCard._split_content_note(text)

    def test_explicit_marker_line(self) -> None:
        content, note = self._split(
            "규제는 강화될 것으로 보인다.\n설명: 결론 마지막 문장을 잇는 한 문장입니다."
        )
        self.assertEqual(content, "규제는 강화될 것으로 보인다.")
        self.assertIn("결론 마지막 문장", note)

    def test_inline_marker_mid_line(self) -> None:
        content, note = self._split("규제는 강화될 것이다. 설명: 부연 설명입니다.")
        self.assertEqual(content, "규제는 강화될 것이다.")
        self.assertEqual(note, "부연 설명입니다.")

    def test_pure_continuation_is_all_content(self) -> None:
        content, note = self._split("규제는 강화될 것으로 보인다.")
        self.assertEqual(content, "규제는 강화될 것으로 보인다.")
        self.assertEqual(note, "")

    def test_blank_line_fallback_peels_trailing_explanation(self) -> None:
        content, note = self._split(
            "규제는 강화될 것으로 보인다.\n\n이 문장은 직전 결론을 보강합니다."
        )
        self.assertEqual(content, "규제는 강화될 것으로 보인다.")
        self.assertIn("보강", note)

    def test_bulleted_review_list_is_not_split(self) -> None:
        review = "- 첫째 지적입니다.\n\n- 둘째 지적입니다."
        content, note = self._split(review)
        # 리뷰 리스트는 통째로 content로 유지(빈줄 fallback이 쪼개면 안 됨).
        self.assertEqual(note, "")
        self.assertIn("첫째", content)
        self.assertIn("둘째", content)

    def test_long_lead_is_not_split(self) -> None:
        long_lead = "가" * 260
        content, note = self._split(f"{long_lead}\n\n짧은 꼬리.")
        self.assertEqual(note, "")
        self.assertIn(long_lead, content)

    def test_citation_commentary_peeled(self) -> None:
        content, note = self._split(
            "규제는 강화될 것이다.\n\n[Document 017] 참조로 출처를 확인하세요."
        )
        self.assertEqual(content, "규제는 강화될 것이다.")
        self.assertIn("[Document 017]", note)


if __name__ == "__main__":
    unittest.main()
