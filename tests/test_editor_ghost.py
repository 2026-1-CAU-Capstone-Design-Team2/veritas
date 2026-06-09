"""Inline ghost-writing behavior of the native editor (MarkdownSourceEdit).

Locks the post-redesign contract:
- the suggestion is INSERTED as a grey span at the caret (so text after the
  caret reflows / is pushed aside) rather than painted over it;
- ``document_text()`` strips the un-accepted span (previews / save / word count /
  next-suggestion prefix never see it), while ``toPlainText()`` contains it;
- Tab accepts (text persists as normal), Esc / 다시 / any other key rejects
  (text removed; ghostDismissed/ghostRetryRequested carry the rejected text);
- there is no type-along.

Skipped automatically when a (headless) QApplication can't be created.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:  # pragma: no cover - environment dependent
    from PySide6.QtWidgets import QApplication

    from frontend.ui.windows.editor_window import MarkdownSourceEdit

    _APP = QApplication.instance() or QApplication([])
    _QT_OK = True
except Exception:  # pragma: no cover - no Qt / no offscreen platform
    _QT_OK = False


@unittest.skipUnless(_QT_OK, "PySide6 offscreen QApplication unavailable")
class InlineGhostTests(unittest.TestCase):
    def _editor(self, text: str = "", caret: int | None = None) -> "MarkdownSourceEdit":
        editor = MarkdownSourceEdit()
        editor.setPlainText(text)
        cursor = editor.textCursor()
        cursor.setPosition(len(text) if caret is None else caret)
        editor.setTextCursor(cursor)
        return editor

    def test_final_ghost_is_inserted_but_stripped_from_document_text(self) -> None:
        editor = self._editor("문장 하나. ")
        editor.set_ghost("이어지는 제안 문장.", final=True)
        # The grey span lives in the document so following text reflows...
        self.assertIn("이어지는 제안 문장.", editor.toPlainText())
        # ...but document_text() (preview / save / word count / prefix) hides it.
        self.assertEqual(editor.document_text(), "문장 하나. ")
        self.assertTrue(editor.has_ghost())

    def test_mid_paragraph_ghost_pushes_following_text(self) -> None:
        # Caret in the MIDDLE of a paragraph: the suggestion must be inserted
        # between head and tail (head + ghost + tail), proving it pushes the
        # following text instead of overlapping it. This is Bug 1b.
        editor = self._editor("머리말. 꼬리말.", caret=len("머리말. "))
        editor.set_ghost("삽입된 제안. ", final=True)
        self.assertEqual(editor.toPlainText(), "머리말. 삽입된 제안. 꼬리말.")
        self.assertEqual(editor.document_text(), "머리말. 꼬리말.")

    def test_accept_persists_suggestion_as_real_text(self) -> None:
        editor = self._editor("앞 문장. ")
        accepted: list[bool] = []
        editor.ghostAccepted.connect(lambda: accepted.append(True))
        editor.set_ghost("받아들인 문장.", final=True)
        editor.accept_ghost()
        self.assertFalse(editor.has_ghost())
        self.assertEqual(editor.document_text(), "앞 문장. 받아들인 문장.")
        self.assertEqual(editor.toPlainText(), "앞 문장. 받아들인 문장.")
        self.assertTrue(accepted)

    def test_reject_removes_suggestion_and_reports_text(self) -> None:
        editor = self._editor("앞 문장. ")
        rejected: list[str] = []
        editor.ghostDismissed.connect(rejected.append)
        editor.set_ghost("거절될 문장.", final=True)
        editor._dismiss_ghost()
        self.assertFalse(editor.has_ghost())
        # Fully removed from the document — no grey residue.
        self.assertEqual(editor.toPlainText(), "앞 문장. ")
        self.assertEqual(rejected, ["거절될 문장."])

    def test_retry_reports_text_and_clears(self) -> None:
        editor = self._editor("앞. ")
        retried: list[str] = []
        editor.ghostRetryRequested.connect(retried.append)
        editor.set_ghost("다시 받을 문장.", final=True)
        editor._request_retry()
        self.assertFalse(editor.has_ghost())
        self.assertEqual(editor.toPlainText(), "앞. ")
        self.assertEqual(retried, ["다시 받을 문장."])

    def test_streaming_defers_document_mutation_until_final(self) -> None:
        editor = self._editor("본문 ")
        editor.set_generating()
        editor.set_ghost("부분", final=False)  # streaming → no document change yet
        self.assertEqual(editor.toPlainText(), "본문 ")
        editor.set_ghost("부분 완성.", final=True)
        self.assertIn("부분 완성.", editor.toPlainText())
        self.assertEqual(editor.document_text(), "본문 ")

    def test_clear_ghost_is_silent(self) -> None:
        editor = self._editor("본문. ")
        signals: list[str] = []
        editor.ghostDismissed.connect(lambda t: signals.append("dismiss"))
        editor.ghostAccepted.connect(lambda: signals.append("accept"))
        editor.set_ghost("임시 제안.", final=True)
        editor.clear_ghost()  # focus-loss / quick-action path: no feedback
        self.assertFalse(editor.has_ghost())
        self.assertEqual(editor.toPlainText(), "본문. ")
        self.assertEqual(signals, [])

    def test_no_type_along(self) -> None:
        editor = self._editor("x")
        self.assertFalse(editor.is_typing_along())
        editor.set_ghost("제안.", final=True)
        self.assertFalse(editor.is_typing_along())


if __name__ == "__main__":
    unittest.main()
