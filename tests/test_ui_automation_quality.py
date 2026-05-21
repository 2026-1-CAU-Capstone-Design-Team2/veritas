import unittest

from services.screen_tool_funcs import UiAutomationReader, UiAutomationResult, WindowContext


class UiAutomationQualityTests(unittest.TestCase):
    def test_registered_editing_apps_short_structured_text_is_primary(self) -> None:
        reader = UiAutomationReader()
        for process_name in (
            "notepad.exe",
            "notepad++.exe",
            "winword.exe",
            "hwp.exe",
            "code.exe",
            "pycharm64.exe",
        ):
            with self.subTest(process_name=process_name):
                window = WindowContext(process_name=process_name)
                result = UiAutomationResult(
                    control_type="DocumentControl",
                    class_name="RichEditD2DPT",
                    focused_name="Text editor",
                    text="car state,",
                    text_source="text_pattern",
                )

                quality, reject_reason = reader._judge_source_quality(result, window)

                self.assertEqual(quality, "primary")
                self.assertIsNone(reject_reason)

    def test_registered_title_extension_short_structured_text_is_primary(self) -> None:
        reader = UiAutomationReader()
        window = WindowContext(process_name="unknown.exe", window_title="draft.md")
        result = UiAutomationResult(
            control_type="EditControl",
            class_name="",
            focused_name="Text editor",
            text="short",
            text_source="value_pattern",
        )

        quality, reject_reason = reader._judge_source_quality(result, window)

        self.assertEqual(quality, "primary")
        self.assertIsNone(reject_reason)

    def test_generic_short_document_text_stays_weak(self) -> None:
        reader = UiAutomationReader()
        window = WindowContext(process_name="unknown.exe")
        result = UiAutomationResult(
            control_type="DocumentControl",
            class_name="RichEditD2DPT",
            focused_name="Text editor",
            text="short",
            text_source="text_pattern",
        )

        quality, reject_reason = reader._judge_source_quality(result, window)

        self.assertEqual(quality, "weak")
        self.assertEqual(reject_reason, "too_short_for_document_context")

    def test_registered_app_control_name_only_stays_weak(self) -> None:
        reader = UiAutomationReader()
        window = WindowContext(process_name="notepad.exe")
        result = UiAutomationResult(
            control_type="DocumentControl",
            class_name="RichEditD2DPT",
            focused_name="Text editor",
            text="short",
            text_source="control_name",
        )

        quality, reject_reason = reader._judge_source_quality(result, window)

        self.assertEqual(quality, "weak")
        self.assertEqual(reject_reason, "too_short_for_document_context")

    def test_registered_app_editor_label_only_is_empty_editor(self) -> None:
        reader = UiAutomationReader()
        window = WindowContext(process_name="notepad.exe")
        result = UiAutomationResult(
            control_type="DocumentControl",
            class_name="RichEditD2DPT",
            focused_name="Text editor",
            text="Text editor",
            text_source="control_name",
        )

        self.assertTrue(reader._is_control_name_only_empty_editor(result, window))

    def test_code_editor_terminal_focus_is_rejected(self) -> None:
        reader = UiAutomationReader()
        window = WindowContext(process_name="code.exe")
        result = UiAutomationResult(
            control_type="DocumentControl",
            class_name="",
            focused_name="Terminal",
            text="short",
            text_source="text_pattern",
        )

        quality, reject_reason = reader._judge_source_quality(result, window)

        self.assertEqual(quality, "rejected")
        self.assertEqual(reject_reason, "code_editor_non_editor_focus")


if __name__ == "__main__":
    unittest.main()
