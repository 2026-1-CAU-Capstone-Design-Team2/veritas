import unittest

from agent.chat_agent import detect_korean_style


class KoreanStyleDetectionTests(unittest.TestCase):
    def test_plain_declarative_written_style(self) -> None:
        text = "기아란 굶주림이 계속되어 삶을 영위하기 어려운 상태를 뜻한다. 아프리카가 대부분을 차지한다."
        self.assertIn("평서체", detect_korean_style(text))

    def test_formal_polite_hapsyo_style(self) -> None:
        # '제출합니다'처럼 ㅂ이 음절에 결합된 합쇼체도 잡혀야 한다.
        text = "안녕하세요. 이번 보고서를 제출합니다. 검토 부탁드립니다."
        self.assertIn("합쇼체", detect_korean_style(text))

    def test_informal_polite_haeyo_style(self) -> None:
        text = "오늘은 기아에 대해 알아볼 거예요. 같이 정리해요."
        self.assertIn("해요체", detect_korean_style(text))

    def test_note_eumseum_style(self) -> None:
        text = "기아 원인 분석함. 분쟁이 주요 요인임. 추가 자료 필요함."
        self.assertIn("음슴체", detect_korean_style(text))

    def test_non_korean_or_too_short_returns_empty(self) -> None:
        self.assertEqual(detect_korean_style("This is an English document."), "")
        self.assertEqual(detect_korean_style("기아."), "")


if __name__ == "__main__":
    unittest.main()
