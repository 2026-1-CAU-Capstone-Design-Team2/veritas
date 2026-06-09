"""Qt stylesheet builders, parameterised by a colour palette.

Each ``build_*`` function returns the QSS for one top-level window, rendered
from a palette dict (``theme.palette()``). The three frameless windows keep
*separate* stylesheets — rather than one app-wide sheet — because the assist
window paints a translucent rounded panel and must not receive a blanket
``QWidget { background-color }`` rule.

Templates use ``%(token)s`` placeholders (mapping-style ``%`` formatting), which
tolerates the dotted token names; the only constraint is that a literal ``%``
in the QSS would need doubling — there are none.

``FONT_STACK`` / ``EDITOR_FONT_STACK`` are factored out so the family list lives
in one place.
"""

from __future__ import annotations

FONT_STACK = "'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif"
EDITOR_FONT_STACK = "'Pretendard', 'Segoe UI Variable', 'Segoe UI', 'Malgun Gothic', 'Noto Sans KR', sans-serif"


def chat_surface_gradient(p: dict[str, str]) -> str:
	"""The soft vertical gradient painted behind chat bubbles / suggestion cards.

	Exposed as a helper because a couple of widgets set it inline (their scroll
	body must paint something opaque or it composites to black on the
	translucent assist window)."""
	return (
		"qlineargradient(x1:0, y1:0, x2:0, y2:1, "
		f"stop:0 {p['chat.surface.start']}, stop:1 {p['chat.surface.end']})"
	)


# --------------------------------------------------------------------------
# Shared fragments — appended to the editor and assist window sheets so all
# three frameless windows share one chrome look.
# --------------------------------------------------------------------------
_TITLEBAR_TMPL = """
	QFrame#VeritasTitleBar {
		background-color: %(titlebar.bg)s;
		border-top-left-radius: 16px;
		border-top-right-radius: 16px;
		border-bottom: 1px solid %(titlebar.border)s;
	}
	/* Maximised (native snap / maximise button): the panel fills the screen, so
	   square off the title-bar corners to match. */
	QFrame#VeritasTitleBar[maximized="true"] {
		border-top-left-radius: 0px;
		border-top-right-radius: 0px;
	}
	QLabel#VeritasTitleBrand { color: %(titlebar.brand)s; font-size: 13px; font-weight: 850; letter-spacing: 1px; }
	QLabel#VeritasTitleSub { color: %(titlebar.sub)s; font-size: 11px; font-weight: 650; }
	QLabel#VeritasTitleSep { color: %(titlebar.sep)s; font-size: 12px; }
	QPushButton#WinCtlButton, QPushButton#WinCtlCloseButton {
		background-color: transparent; border: none; border-radius: 6px;
	}
	QPushButton#WinCtlButton:hover { background-color: %(titlebar.winctl.hover)s; }
	QPushButton#WinCtlCloseButton:hover { background-color: %(titlebar.close.hover)s; }
"""

_CHAT_TMPL = """
	QFrame#AssistPagePanel { background-color: %(surface.muted)s; border: 1px solid %(border.gray)s; border-radius: 16px; }
	QFrame#AssistSectionCard { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %(chat.surface.start)s, stop:1 %(chat.surface.end)s); border: 1px solid %(border.gray)s; border-radius: 13px; }
	QLabel#AssistSectionTitle { color: %(text.primary2)s; font-size: 13px; font-weight: 850; }
	QScrollArea#AssistScrollArea { background-color: transparent; border: none; }
	QScrollArea#ChatScroll { background: transparent; border: none; }
	QWidget#AssistScrollBody { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %(chat.surface.start)s, stop:1 %(chat.surface.end)s); }
	QWidget#ChatScrollBody { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %(chat.surface.start)s, stop:1 %(chat.surface.end)s); }
	QLabel#AssistEmptyState { background-color: %(surface.muted)s; border: 1px dashed %(border.strong)s; border-radius: 12px; color: %(text.secondary.gray)s; padding: 18px 14px; font-weight: 650; }
	QPushButton#AssistCopyButton { background-color: %(surface)s; color: %(text.subtle)s; border: 1px solid %(border.gray.strong)s; border-radius: 8px; padding: 5px 8px; font-size: 11px; font-weight: 800; }
	QPushButton#AssistCopyButton:hover { background-color: %(surface.muted2)s; color: %(text.primary2)s; }
	QFrame#AssistUserBubble { background-color: %(bubble.user.bg)s; border: 1px solid %(bubble.user.border)s; border-radius: 13px; border-top-right-radius: 4px; }
	QFrame#AssistAiBubble { background-color: %(bubble.ai.bg)s; border: 1px solid %(bubble.ai.border)s; border-radius: 13px; border-top-left-radius: 4px; }
	QLabel#AssistBubbleMeta { color: %(text.secondary.gray)s; font-size: 10px; font-weight: 800; }
	QTextBrowser#AssistBubbleText { color: %(text.body)s; font-size: 12px; font-weight: 600; background: transparent; border: none; }
	QFrame#AssistInputBar { background-color: %(surface)s; border: 1px solid %(border.gray)s; border-radius: 14px; }
	QTextEdit#AssistChatInput { background-color: %(surface.muted)s; border: 1px solid %(border.gray)s; border-radius: 11px; padding: 8px 10px; color: %(text.primary2)s; selection-background-color: %(selection.bg)s; selection-color: %(text.primary2)s; }
	QTextEdit#AssistChatInput:focus { background-color: %(surface)s; border: 1px solid %(blue)s; }
	QPushButton#AssistSendButton { background-color: %(send.flat.bg)s; border: 1px solid %(send.flat.border)s; border-radius: 11px; color: %(text.on_accent)s; font-weight: 850; }
	QPushButton#AssistSendButton:hover { background-color: %(blue.hover)s; }
	QPushButton#AssistModeButton { background-color: %(surface.inset)s; color: %(text.slate600)s; border: 1px solid %(border.gray.strong)s; border-radius: 11px; padding: 0px; font-size: 13px; font-weight: 800; }
	QPushButton#AssistModeButton:hover { background-color: %(accent.subtle.bg.hover)s; border-color: %(accent.border.checked)s; color: %(accent.text)s; }
	QPushButton#AssistModeButton[researchActive="true"] { background-color: %(deepblue.start)s; border-color: %(deepblue.start)s; color: %(text.on_accent)s; }
	QPushButton#AssistModeButton[researchActive="true"]:hover { background-color: %(deepblue.end)s; border-color: %(deepblue.end)s; color: %(text.on_accent)s; }
"""


def titlebar_qss(p: dict[str, str]) -> str:
	return _TITLEBAR_TMPL % p


def chat_qss(p: dict[str, str]) -> str:
	return _CHAT_TMPL % p


# --------------------------------------------------------------------------
# Main window (covers every in-window page through the cascade).
# --------------------------------------------------------------------------
_MAIN_TMPL = """
	QWidget {
		background-color: %(bg.base)s;
		color: %(text.primary2)s;
		font-family: """ + FONT_STACK + """;
		font-size: 13px;
		font-weight: 500;
	}

	QLabel {
		background-color: transparent;
		selection-background-color: %(selection.bg)s;
		selection-color: %(selection.text)s;
	}

	QPushButton {
		background-color: %(button.bg)s;
		color: %(text.on_dark)s;
		border: 1px solid %(button.bg)s;
		border-radius: 8px;
		padding: 8px 12px;
		font-family: """ + FONT_STACK + """;
		font-size: 13px;
		font-weight: 700;
	}

	QPushButton:hover {
		background-color: %(button.bg.hover)s;
	}

	/* Frameless main window: the top-level widget + central root stay transparent
	   so the translucent rounded panel (and its drop shadow) can show through. */
	QMainWindow { background-color: transparent; }

	QWidget#AppRoot {
		background-color: transparent;
	}

	QFrame#AppPanel {
		background-color: qlineargradient(
			x1: 0, y1: 0, x2: 0, y2: 1,
			stop: 0 %(bg.app.start)s,
			stop: 1 %(bg.app.end)s
		);
		border-radius: 16px;
	}

	/* When maximized the panel fills the screen, so square off its corners. */
	QFrame#AppPanel[maximized="true"] { border-radius: 0px; }
	QFrame#VeritasTitleBar[maximized="true"] {
		border-top-left-radius: 0px;
		border-top-right-radius: 0px;
	}

	QFrame#Sidebar {
		background-color: %(sidebar.bg)s;
		border-radius: 18px;
		border: 1px solid %(sidebar.border)s;
	}

	QLabel#BrandLabel {
		color: %(sidebar.brand)s;
		font-family: """ + FONT_STACK + """;
		font-size: 19px;
		font-weight: 800;
		letter-spacing: -0.2px;
	}

	QLabel#BrandSubLabel {
		color: %(sidebar.brand.sub)s;
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.3px;
	}

	QFrame#SidebarFooterCard {
		background-color: %(sidebar.footer.bg)s;
		border: 1px solid %(sidebar.footer.border)s;
		border-radius: 11px;
	}

	QLabel#SidebarFooterTitle {
		color: %(sidebar.footer.title)s;
		font-size: 11px;
		font-weight: 800;
		letter-spacing: 0.3px;
	}

	QLabel#SidebarFooterDesc {
		color: %(sidebar.footer.desc)s;
		font-size: 14px;
		font-weight: 700;
	}

	QPushButton#SidebarWorkspaceButton {
		background-color: %(sidebar.wsbtn.bg)s;
		color: %(sidebar.wsbtn.text)s;
		border: 1px solid %(sidebar.wsbtn.border)s;
		border-radius: 8px;
		padding: 6px 10px;
		font-size: 11px;
		font-weight: 700;
	}

	QPushButton#SidebarWorkspaceButton:hover {
		background-color: %(sidebar.wsbtn.bg.hover)s;
		border-color: %(sidebar.wsbtn.border.hover)s;
	}

	QFrame#CenterPanel {
		background-color: %(surface)s;
		border: 1px solid %(border)s;
		border-radius: 18px;
	}

	QFrame#TopHero {
		background: qlineargradient(
			x1: 0, y1: 0, x2: 1, y2: 0,
			stop: 0 %(hero.start)s,
			stop: 1 %(hero.end)s
		);
		border: 1px solid %(hero.border)s;
		border-radius: 14px;
	}

	QLabel#SectionTitle {
		color: %(hero.title)s;
		font-family: """ + FONT_STACK + """;
		font-size: 21px;
		font-weight: 800;
		letter-spacing: -0.1px;
	}

	QLabel#SectionDesc {
		color: %(hero.desc)s;
		font-size: 12px;
		font-weight: 600;
	}

	QLabel#StageChip {
		background-color: %(hero.chip.bg)s;
		border: 1px solid %(hero.chip.border)s;
		border-radius: 12px;
		color: %(hero.chip.text)s;
		font-size: 11px;
		font-weight: 700;
		padding: 5px 10px;
	}

	QPushButton#TopActionButton {
		background-color: %(hero.btn.bg)s;
		color: %(hero.btn.text)s;
		border: 1px solid %(hero.btn.border)s;
		border-radius: 9px;
		padding: 8px 13px;
		font-weight: 700;
	}

	QPushButton#TopActionButton:hover {
		background-color: %(hero.btn.bg.hover)s;
	}

	QPushButton#SidebarCollapseButton {
		background-color: %(hero.btn.bg)s;
		color: %(hero.btn.text)s;
		border: 1px solid %(hero.btn.border)s;
		border-radius: 10px;
		font-size: 15px;
		font-weight: 700;
		padding: 0px;
	}

	QPushButton#SidebarCollapseButton:hover {
		background-color: %(hero.btn.bg.hover)s;
	}

	QFrame#WorkflowStepper {
		background-color: %(surface)s;
		border: 1px solid %(border)s;
		border-radius: 14px;
	}

	QFrame#StepperConnector {
		background-color: %(border.gray.strong)s;
		border-radius: 1px;
	}

	QFrame#RightPanel {
		background-color: %(surface)s;
		border-radius: 16px;
		border: 1px solid %(border)s;
	}

	QFrame#ChatHero {
		background: qlineargradient(
			x1: 0, y1: 0, x2: 1, y2: 0,
			stop: 0 %(chathero.start)s,
			stop: 1 %(chathero.end)s
		);
		border: 1px solid %(chathero.border)s;
		border-radius: 16px;
	}

	QFrame#ChatPanel {
		background-color: %(surface)s;
		border: 1px solid %(border)s;
		border-radius: 16px;
	}

	QFrame#AssistPagePanel {
		background-color: %(surface.muted)s;
		border: 1px solid %(border.gray)s;
		border-radius: 16px;
	}

	QFrame#AssistSectionCard {
		background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %(chat.surface.start)s, stop:1 %(chat.surface.end)s);
		border: 1px solid %(border.gray)s;
		border-radius: 13px;
	}

	QLabel#AssistSubText {
		color: %(text.secondary.gray)s;
		font-size: 12px;
		font-weight: 600;
	}

	QLabel#AssistSectionTitle {
		color: %(text.primary2)s;
		font-size: 13px;
		font-weight: 850;
	}

	QScrollArea#AssistScrollArea {
		background-color: transparent;
		border: none;
	}

	QFrame#SuggestionCard {
		background-color: %(surface)s;
		border: 1px solid %(border.gray)s;
		border-radius: 12px;
	}

	QLabel#SuggestionText {
		color: %(text.body)s;
		font-size: 13px;
		font-weight: 650;
		line-height: 1.5;
	}

	QLabel#AssistEmptyState {
		background-color: %(surface.muted)s;
		border: 1px dashed %(border.strong)s;
		border-radius: 12px;
		color: %(text.secondary.gray)s;
		padding: 18px 14px;
		font-weight: 650;
	}

	QPushButton#AssistCopyButton {
		background-color: %(surface)s;
		color: %(text.subtle)s;
		border: 1px solid %(border.gray.strong)s;
		border-radius: 8px;
		padding: 5px 8px;
		font-size: 11px;
		font-weight: 800;
	}

	QPushButton#AssistCopyButton:hover {
		background-color: %(surface.muted2)s;
		color: %(text.primary2)s;
	}

	QFrame#AssistUserBubble {
		background-color: %(bubble.user.bg)s;
		border: 1px solid %(bubble.user.border)s;
		border-radius: 13px;
		border-top-right-radius: 4px;
	}

	QFrame#AssistAiBubble {
		background-color: %(bubble.ai.bg)s;
		border: 1px solid %(bubble.ai.border)s;
		border-radius: 13px;
		border-top-left-radius: 4px;
	}

	QLabel#AssistBubbleMeta {
		color: %(text.secondary.gray)s;
		font-size: 10px;
		font-weight: 800;
	}

	QTextBrowser#AssistBubbleText {
		color: %(text.body)s;
		font-size: 12px;
		font-weight: 600;
		background: transparent;
		border: none;
	}

	QFrame#AssistInputBar {
		background-color: %(surface)s;
		border: 1px solid %(border.gray)s;
		border-radius: 14px;
	}

	QTextEdit#AssistChatInput {
		background-color: %(surface.muted)s;
		border: 1px solid %(border.gray)s;
		border-radius: 11px;
		padding: 8px 10px;
		color: %(text.primary2)s;
		selection-background-color: %(selection.bg)s;
		selection-color: %(text.primary2)s;
	}

	QTextEdit#AssistChatInput:focus {
		background-color: %(surface)s;
		border: 1px solid %(blue)s;
	}

	QPushButton#AssistSendButton {
		background-color: %(send.flat.bg)s;
		border: 1px solid %(send.flat.border)s;
		border-radius: 11px;
		color: %(text.on_accent)s;
		font-weight: 850;
	}

	QPushButton#AssistSendButton:hover {
		background-color: %(blue.hover)s;
	}

	QPushButton#AssistModeButton {
		background-color: %(surface.inset)s;
		color: %(text.slate600)s;
		border: 1px solid %(border.gray.strong)s;
		border-radius: 11px;
		padding: 0px;
		font-size: 13px;
		font-weight: 800;
	}

	QPushButton#AssistModeButton:hover {
		background-color: %(accent.subtle.bg.hover)s;
		border-color: %(accent.border.checked)s;
		color: %(accent.text)s;
	}

	QPushButton#AssistModeButton[researchActive="true"] {
		background-color: %(deepblue.start)s;
		border-color: %(deepblue.start)s;
		color: %(text.on_accent)s;
	}

	QPushButton#AssistModeButton[researchActive="true"]:hover {
		background-color: %(deepblue.end)s;
		border-color: %(deepblue.end)s;
		color: %(text.on_accent)s;
	}

	QFrame#ComposerCard {
		background-color: %(surface.muted)s;
		border: 1px solid %(border)s;
		border-radius: 18px;
		padding: 8px;
	}

	QFrame#ChatHeroIconBox {
		background-color: %(chathero.iconbox.bg)s;
		border: 1px solid %(chathero.iconbox.border)s;
		border-radius: 14px;
	}

	QLabel#ChatHeroIcon {
		background-color: transparent;
	}

	QScrollArea#ChatScroll {
		background: transparent;
		border: none;
	}

	QWidget#AssistScrollBody {
		background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %(chat.surface.start)s, stop:1 %(chat.surface.end)s);
	}

	QWidget#ChatScrollBody {
		background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %(chat.surface.start)s, stop:1 %(chat.surface.end)s);
	}

	QScrollArea#PageScroll {
		background: transparent;
		border: none;
	}

	QScrollBar:vertical {
		background: transparent;
		width: 10px;
		margin: 2px 0 2px 0;
	}

	QScrollBar::handle:vertical {
		background: %(scrollbar.handle)s;
		border-radius: 5px;
		min-height: 28px;
	}

	QScrollBar::handle:vertical:hover {
		background: %(scrollbar.handle.hover)s;
	}

	QScrollBar::add-line:vertical,
	QScrollBar::sub-line:vertical {
		height: 0px;
	}

	QScrollBar::add-page:vertical,
	QScrollBar::sub-page:vertical {
		background: transparent;
	}

	QScrollBar:horizontal {
		background: transparent;
		height: 10px;
		margin: 0 2px 0 2px;
	}

	QScrollBar::handle:horizontal {
		background: %(scrollbar.handle)s;
		border-radius: 5px;
		min-width: 28px;
	}

	QScrollBar::handle:horizontal:hover {
		background: %(scrollbar.handle.hover)s;
	}

	QScrollBar::add-line:horizontal,
	QScrollBar::sub-line:horizontal {
		width: 0px;
	}

	QScrollBar::add-page:horizontal,
	QScrollBar::sub-page:horizontal {
		background: transparent;
	}

	QLineEdit#ChatInput {
		background-color: %(surface.muted)s;
		border: 1px solid %(border.strong)s;
		border-radius: 10px;
		padding: 10px 11px;
		color: %(text.body)s;
		selection-background-color: %(selection.bg.indigo)s;
		selection-color: %(selection.text)s;
	}

	QPlainTextEdit#ChatInput {
		background-color: %(surface)s;
		border: 1px solid %(border)s;
		border-radius: 16px;
		padding: 9px 13px;
		color: %(text.primary)s;
		selection-background-color: %(selection.bg.violet)s;
		selection-color: %(selection.text)s;
		font-size: 13px;
	}

	QPlainTextEdit#ChatInput:focus {
		border: 1px solid %(focus.border)s;
		background-color: %(surface)s;
	}

	QLineEdit#ChatInput:focus {
		border: 1px solid %(focus.border)s;
		background-color: %(surface)s;
	}

	QPushButton#SendButton {
		background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 %(send.grad.start)s, stop:1 %(send.grad.end)s);
		color: %(text.on_accent)s;
		border: none;
		border-radius: 18px;
		min-width: 44px;
		min-height: 44px;
		padding: 8px 12px;
		font-weight: 700;
		font-size: 13px;
	}

	QPushButton#SendButton:hover {
		background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 %(send.grad.start.hover)s, stop:1 %(send.grad.end.hover)s);
	}

	QToolButton#ModeMenuButton {
		background-color: %(button.bg.hover)s;
		color: %(text.on_accent)s;
		border: 1px solid %(button.bg.hover)s;
		border-radius: 19px;
		padding: 0px;
		font-size: 12px;
		font-weight: 700;
		text-align: center;
		min-width: 82px;
		min-height: 38px;
		max-width: 82px;
		max-height: 38px;
	}

	QToolButton#ModeMenuButton:hover {
		background-color: %(accent)s;
		border-color: %(accent.hover)s;
	}

	QToolButton#ModeMenuButton::menu-indicator {
		image: none;
		width: 0px;
		height: 0px;
	}

	QTextEdit#ResearchInput {
		background-color: %(surface)s;
		border: 1px solid %(border.strong)s;
		border-radius: 12px;
		padding: 11px 12px;
		color: %(text.primary)s;
		selection-background-color: %(selection.bg.indigo)s;
		selection-color: %(selection.text)s;
	}

	QTextEdit#ResearchInput:focus {
		border: 1px solid %(accent)s;
	}

	QFrame#ReferenceUrlRow {
		background-color: %(surface.muted)s;
		border: 1px solid %(border)s;
		border-radius: 12px;
	}

	QLineEdit#ReferenceUrlInput {
		background-color: transparent;
		border: none;
		color: %(text.primary)s;
		padding: 7px 4px;
		font-size: 13px;
	}

	QToolButton#RoundAddButton {
		background-color: %(button.bg.hover)s;
		color: %(text.on_accent)s;
		border: 1px solid %(button.bg.hover)s;
		border-radius: 15px;
		font-size: 17px;
		font-weight: 800;
		padding: 0px;
	}

	QToolButton#RoundAddButton:hover {
		background-color: %(accent)s;
		border-color: %(accent.hover)s;
	}

	QToolButton#UrlRemoveButton {
		background-color: %(surface)s;
		color: %(text.secondary)s;
		border: 1px solid %(border.strong)s;
		border-radius: 13px;
		font-size: 14px;
		font-weight: 800;
		padding: 0px;
	}

	QToolButton#UrlRemoveButton:hover {
		background-color: %(danger.bg)s;
		color: %(danger.fg)s;
		border-color: %(danger.border)s;
	}

	QMenu {
		background-color: %(menu.bg)s;
		border: 1px solid %(menu.border)s;
		border-radius: 8px;
		padding: 6px;
	}

	QMenu::item {
		color: %(menu.item.text)s;
		padding: 8px 28px 8px 12px;
		border-radius: 6px;
	}

	QMenu::item:selected {
		background-color: %(menu.item.sel.bg)s;
		color: %(menu.item.sel.text)s;
	}

	QFrame#UserBubble {
		background-color: %(bubble2.user.bg)s;
		border: 1px solid %(bubble2.user.border)s;
		border-radius: 11px;
		border-top-right-radius: 3px;
	}

	QFrame#AIBubble {
		background-color: %(bubble2.ai.bg)s;
		border: 1px solid %(bubble2.ai.border)s;
		border-radius: 11px;
		border-top-left-radius: 3px;
	}

	QLabel#BubbleText {
		font-size: 14px;
		color: %(bubble2.text)s;
		font-weight: 550;
	}

	QLabel#BubbleMeta {
		font-size: 10px;
		color: %(bubble2.meta)s;
	}

	QFrame#UserBubble QLabel#BubbleText {
		color: %(bubble2.user.text)s;
	}

	QFrame#UserBubble QLabel#BubbleMeta {
		color: %(bubble2.user.meta)s;
	}

	QLabel#ChatContextChip {
		background-color: %(accent.subtle.bg)s;
		color: %(accent.text)s;
		border: 1px solid %(accent.subtle.border)s;
		border-radius: 10px;
		padding: 5px 9px;
	}

	QFrame#WorkflowBadge {
		background-color: %(warning.bg)s;
		border: 1px solid %(warning.border)s;
		border-radius: 10px;
	}

	QLabel#PanelTitle {
		font-size: 16px;
		font-weight: 800;
		color: %(text.body)s;
	}

	QLabel#PanelSubtitle {
		font-size: 12px;
		color: %(text.secondary.gray)s;
	}

	QFrame#CardWidget, QFrame#StatTile {
		background-color: %(surface)s;
		border: 1px solid %(border)s;
		border-radius: 13px;
	}

	QLabel#CardTitle {
		font-size: 14px;
		font-weight: 800;
		color: %(text.primary)s;
	}

	/* CollapsibleCard header (components/cards.py) — chevron glyph + status badge
	   that stays visible while the card body is collapsed. */
	QLabel#CollapsibleChevron {
		font-size: 11px;
		color: %(text.secondary)s;
	}

	QFrame#CollapsibleCardHeader:hover QLabel#CollapsibleChevron,
	QFrame#CollapsibleCardHeader:hover QLabel#CardTitle {
		color: %(accent)s;
	}

	QLabel#CollapsibleStatus {
		font-size: 12px;
		font-weight: 700;
		color: %(success.strong)s;
	}

	QLabel#CollapsibleStatus[tone="warning"] { color: %(warning.fg2)s; }
	QLabel#CollapsibleStatus[tone="danger"] { color: %(danger.fg)s; }
	QLabel#CollapsibleStatus[tone="neutral"] { color: %(text.secondary)s; }

	QLabel#CardPrimary {
		font-size: 13px;
		font-weight: 700;
		color: %(text.primary)s;
	}

	QLabel#CardSecondary {
		font-size: 12px;
		color: %(text.secondary)s;
	}

	QLabel#CardFooter {
		font-size: 11px;
		color: %(text.muted)s;
	}

	QLabel#PageTitle {
		font-size: 24px;
		font-weight: 800;
		color: %(text.primary)s;
		letter-spacing: -0.1px;
	}

	QLabel#PageSubtitle {
		font-size: 13px;
		color: %(text.secondary)s;
		font-weight: 600;
	}

	QLabel#IssueText {
		font-size: 13px;
		font-weight: 700;
		color: %(danger.fg.strong)s;
	}

	QLabel#WarningSummary {
		background-color: %(warning.bg)s;
		border: 1px solid %(warning.border)s;
		border-radius: 10px;
		color: %(warning.fg3)s;
		padding: 10px 11px;
		font-weight: 700;
		font-size: 12px;
	}

	QLabel#StatLabel {
		font-size: 11px;
		color: %(text.muted)s;
		font-weight: 700;
		letter-spacing: 0.3px;
	}

	QLabel#StatValue {
		font-size: 28px;
		color: %(text.primary)s;
		font-weight: 800;
		letter-spacing: -0.5px;
	}

	QLabel#StatDelta {
		font-size: 12px;
		color: %(stat.delta)s;
		font-weight: 700;
	}

	QPushButton#PrimaryButton {
		background-color: %(accent)s;
		color: %(text.on_accent)s;
		border: 1px solid %(accent.hover)s;
		border-radius: 10px;
		padding: 10px 14px;
		font-weight: 700;
	}

	QPushButton#PrimaryButton:hover {
		background-color: %(accent.hover)s;
	}

	QPushButton#GhostButton {
		background-color: %(surface)s;
		color: %(text.strong)s;
		border: 1px solid %(border.strong)s;
		border-radius: 9px;
		padding: 8px 12px;
		font-weight: 700;
	}

	QPushButton#GhostButton:hover {
		background-color: %(surface.muted)s;
		border-color: %(border.hover)s;
	}

	QPushButton#VerifyDetailButton {
		background-color: %(surface)s;
		color: %(text.strong)s;
		border: 1px solid %(border.strong)s;
		border-radius: 8px;
		padding: 4px 8px;
		font-size: 11px;
		font-weight: 700;
	}

	QPushButton#VerifyDetailButton:hover {
		background-color: %(surface.muted)s;
		border-color: %(border.hover)s;
	}

	QPushButton#FilterChip {
		background-color: %(surface)s;
		color: %(text.strong)s;
		border: 1px solid %(border.strong)s;
		border-radius: 14px;
		padding: 7px 13px;
		font-size: 11px;
		font-weight: 700;
	}

	QPushButton#FilterChip:hover {
		background-color: %(surface.muted)s;
		border-color: %(border.hover)s;
	}

	QPushButton#FilterChip:checked {
		background-color: %(accent.subtle.bg)s;
		color: %(accent.text)s;
		border: 1px solid %(accent.border.checked)s;
	}

	QTextEdit#DocEditor {
		background-color: %(surface)s;
		border: 1px solid %(border.strong)s;
		border-radius: 12px;
		padding: 13px;
		font-size: 13px;
		line-height: 1.6;
		color: %(text.body)s;
		selection-background-color: %(selection.bg)s;
		selection-color: %(selection.text)s;
	}

	QComboBox#SettingsInput,
	QLineEdit#SettingsInput,
	QSpinBox#SettingsInput,
	QDoubleSpinBox#SettingsInput {
		background-color: %(surface.muted)s;
		border: 1px solid %(border.strong)s;
		border-radius: 9px;
		padding: 7px 10px;
		color: %(text.primary2)s;
		min-height: 24px;
	}

	QComboBox#SettingsInput:focus,
	QLineEdit#SettingsInput:focus,
	QSpinBox#SettingsInput:focus,
	QDoubleSpinBox#SettingsInput:focus {
		border: 1px solid %(accent)s;
		background-color: %(surface)s;
	}

	/* combobox-popup: 0 keeps the list a normal drop-down anchored below the
	   field instead of the menu-style popup that floats over / above the box
	   and scrolls to the current selection. The native drop-down button + 3D
	   arrow are hidden; _SettingsCombo hand-paints a flat chevron instead. */
	QComboBox#SettingsInput {
		combobox-popup: 0;
		padding-right: 28px;
	}

	QComboBox#SettingsInput::drop-down {
		subcontrol-origin: padding;
		subcontrol-position: top right;
		width: 26px;
		border: none;
		background: transparent;
	}

	QComboBox#SettingsInput::down-arrow {
		image: none;
		width: 0px;
		height: 0px;
	}

	QComboBox#SettingsInput QAbstractItemView {
		background-color: %(menu.bg)s;
		border: 1px solid %(menu.border)s;
		border-radius: 10px;
		padding: 6px;
		outline: none;
		selection-background-color: %(accent.subtle.bg)s;
		selection-color: %(accent.text)s;
	}

	QComboBox#SettingsInput QAbstractItemView::item {
		border: none;
		border-radius: 6px;
		padding: 7px 10px;
		min-height: 22px;
		color: %(text.primary2)s;
	}

	QComboBox#SettingsInput QAbstractItemView::item:selected {
		background-color: %(accent.subtle.bg)s;
		color: %(accent.text)s;
	}

	QCheckBox#SettingsCheckbox {
		color: %(text.strong)s;
		font-weight: 700;
		spacing: 8px;
	}

	QPushButton#SettingsModelToggle {
		background-color: %(surface)s;
		color: %(text.strong)s;
		border: 1px solid %(border.strong)s;
		border-radius: 10px;
		padding: 9px 14px;
		font-weight: 800;
	}

	QPushButton#SettingsModelToggle:hover {
		background-color: %(surface.muted)s;
		border-color: %(border.hover)s;
	}

	QPushButton#SettingsModelToggle:checked {
		background-color: %(accent.subtle.bg)s;
		color: %(accent.text)s;
		border: 1px solid %(accent.border.checked)s;
	}

	QListWidget#SettingsFolderList {
		background-color: %(surface.muted)s;
		border: 1px solid %(border.strong)s;
		border-radius: 10px;
		padding: 6px;
		color: %(text.primary)s;
		selection-background-color: %(info.bg)s;
		selection-color: %(selection.text)s;
	}

	QListWidget#SettingsFolderList::item {
		border-radius: 7px;
		padding: 7px 8px;
		margin: 2px;
	}

	QListWidget#SettingsFolderList::item:selected {
		background-color: %(info.bg)s;
		color: %(selection.text)s;
	}

	QLabel#SettingsStatus {
		background-color: %(surface.muted)s;
		border: 1px solid %(border)s;
		border-radius: 10px;
		color: %(text.slate600)s;
		padding: 10px 11px;
		font-size: 12px;
		font-weight: 700;
	}

	QFrame#ResearchCountCard {
		background-color: %(surface.muted)s;
		border: 1px solid %(border)s;
		border-radius: 12px;
	}

	QFrame#DocCountStepper {
		background-color: %(surface)s;
		border: 1px solid %(border)s;
		border-radius: 22px;
	}

	QToolButton#StepperButton {
		background-color: %(accent.subtle.bg)s;
		border: 1px solid %(accent.subtle.bg.hover)s;
		border-radius: 16px;
	}

	QToolButton#StepperButton:hover {
		background-color: %(accent.subtle.bg.hover)s;
		border-color: %(accent.subtle.border)s;
	}

	QToolButton#StepperButton:pressed {
		background-color: %(accent.subtle.border)s;
	}

	QToolButton#StepperButton:disabled {
		background-color: %(surface.inset)s;
		border-color: %(border)s;
	}

	QLineEdit#StepperValue {
		font-size: 15px;
		font-weight: 800;
		color: %(text.primary)s;
		background: transparent;
		border: none;
		padding: 0px;
	}

	QLineEdit#StepperValue:focus {
		background: %(accent.subtle.bg)s;
		border-radius: 6px;
	}

	QLabel#StepperUnit {
		font-size: 13px;
		font-weight: 700;
		color: %(text.secondary)s;
	}

	QLabel#ResearchCountTitle {
		font-size: 13px;
		font-weight: 800;
		color: %(text.primary)s;
	}

	QLabel#ResearchCountHint {
		font-size: 11px;
		font-weight: 600;
		color: %(text.muted)s;
	}

	QLabel#ToolChip {
		background-color: %(accent.subtle.bg)s;
		color: %(accent.text)s;
		border: 1px solid %(accent.subtle.border)s;
		border-radius: 13px;
		padding: 5px 12px;
		font-size: 11px;
		font-weight: 700;
	}

	QFrame#DocToolAddRow {
		background-color: %(surface.muted)s;
		border: 1px solid %(border)s;
		border-radius: 12px;
	}

	QLabel#FieldLabel {
		font-size: 11px;
		font-weight: 700;
		color: %(text.secondary)s;
		letter-spacing: 0.2px;
	}

	QPushButton#AdvancedToggleHeader {
		background-color: transparent; border: none; text-align: left;
		padding: 0px; color: %(text.primary)s; font-size: 14px; font-weight: 800;
	}

	QPushButton#AdvancedToggleHeader:hover { color: %(accent)s; }

	QLabel#SettingsSubsectionTitle {
		font-size: 13px; font-weight: 800; color: %(text.primary)s;
	}

	QFrame#SettingsDivider { background-color: %(border)s; border: none; }
"""


# --------------------------------------------------------------------------
# Editor window (Google-Docs-like chrome). Self-applied, so its PrimaryButton /
# GhostButton / QMenu rules scope to the editor only.
# --------------------------------------------------------------------------
_EDITOR_TMPL = """
	QWidget {
		font-family: """ + EDITOR_FONT_STACK + """;
		font-size: 13px;
		color: %(editor.text)s;
	}
	QFrame#EditorPanel { background-color: %(editor.surface)s; border: 1px solid %(editor.border)s; border-radius: 12px; }
	QFrame#EditorPanel[maximized="true"] { border-radius: 0px; }
	QFrame#EditorTitleBar {
		background-color: %(editor.bar)s; border-top-left-radius: 12px; border-top-right-radius: 12px;
		border-bottom: 1px solid %(editor.border.soft)s;
	}
	QLabel#EditorDocTitle { font-size: 14px; font-weight: 700; color: %(editor.text)s; }
	QLabel#EditorSaveStatus { font-size: 11px; font-weight: 600; color: %(editor.text.secondary)s; }
	QPushButton#EditorWinButton, QPushButton#EditorCloseButton {
		background-color: transparent; color: %(editor.text.secondary)s; border: none; border-radius: 6px;
		font-size: 14px; font-weight: 700;
	}
	QPushButton#EditorWinButton:hover { background-color: %(editor.border.soft)s; color: %(editor.text)s; }
	QPushButton#EditorCloseButton:hover { background-color: %(editor.close.hover.bg)s; color: %(editor.close.hover.fg)s; }
	QFrame#EditorMenuRow { background-color: %(editor.surface)s; border-bottom: 1px solid %(editor.border.softer)s; }
	QToolButton#EditorMenuButton {
		background-color: transparent; color: %(editor.text.tertiary)s; border: none; border-radius: 6px;
		padding: 5px 5px; font-weight: 600;
	}
	QToolButton#EditorMenuButton:hover { background-color: %(editor.hover)s; }
	QToolButton#EditorMenuButton::menu-indicator { image: none; width: 0; }
	QToolButton#EditorExportButton {
		background-color: %(editor.accent)s; color: %(text.on_accent)s; border: none; border-radius: 8px;
		padding: 6px 10px; font-weight: 700;
	}
	QToolButton#EditorExportButton:hover { background-color: %(editor.accent.hover)s; }
	QToolButton#EditorExportButton::menu-indicator { image: none; width: 0; }
	QFrame#EditorToolbar { background-color: %(editor.surface)s; border-bottom: 1px solid %(editor.border.soft)s; }
	QPushButton#EditorToolButton {
		background-color: %(editor.surface)s; color: %(editor.text.tertiary)s; border: 1px solid %(editor.border.soft)s; border-radius: 6px;
		padding: 0px 9px; font-weight: 700; min-width: 22px;
	}
	QPushButton#EditorToolButton:hover { background-color: %(editor.hover)s; border-color: %(editor.border)s; }
	QPushButton#EditorIconButton {
		background-color: %(editor.surface)s; border: 1px solid %(editor.border.soft)s; border-radius: 6px; padding: 0px;
	}
	QPushButton#EditorIconButton:hover { background-color: %(editor.hover)s; border-color: %(editor.border)s; }
	QToolButton#EditorStyleButton {
		background-color: %(editor.surface)s; color: %(editor.text.tertiary)s; border: 1px solid %(editor.border.soft)s; border-radius: 6px;
		padding: 3px 10px; font-weight: 600;
	}
	QToolButton#EditorStyleButton:hover { background-color: %(editor.hover)s; border-color: %(editor.border)s; }
	QToolButton#EditorStyleButton::menu-indicator { image: none; width: 0; }
	QFrame#EditorToolSep { background-color: %(editor.border.soft)s; }
	QPushButton#ViewToggleButton {
		background-color: %(editor.surface)s; color: %(editor.text.secondary)s; border: 1px solid %(editor.border)s; border-radius: 6px;
		padding: 3px 12px; font-weight: 600;
	}
	QPushButton#ViewToggleButton:checked { background-color: %(editor.accent.subtle)s; color: %(editor.accent)s; border-color: %(editor.accent)s; }
	QPushButton#PanelToggleButton {
		background-color: %(editor.surface)s; color: %(editor.text.secondary)s; border: 1px solid %(editor.border)s; border-radius: 6px;
		padding: 3px 10px; font-weight: 600;
	}
	QPushButton#PanelToggleButton:checked { background-color: %(editor.accent.subtle)s; color: %(editor.accent)s; border-color: %(editor.accent)s; }
	QPushButton#PanelToggleButton:hover { background-color: %(editor.hover)s; }
	QSplitter#EditorMainSplit::handle { background-color: %(editor.border.soft)s; }
	QSplitter#CenterSplit { background-color: %(editor.canvas)s; }
	QSplitter#CenterSplit::handle { background-color: %(editor.canvas)s; }
	QFrame#EditorCanvas { background-color: %(editor.canvas)s; }
	QFrame#EditorPage { background-color: %(editor.surface)s; border: 1px solid %(editor.border.soft)s; border-radius: 4px; }
	QTextEdit#EditorSource {
		background-color: %(editor.surface)s; color: %(editor.text)s; border: none; border-radius: 4px;
		padding: 30px 40px; font-size: 15px;
		selection-background-color: %(editor.selection)s; selection-color: %(editor.text)s;
	}
	QTextBrowser#EditorPreview {
		background-color: %(editor.surface)s; color: %(editor.text)s; border: none; border-radius: 4px; padding: 24px 32px;
	}
	QFrame#OutlinePanel, QFrame#AssistPanel { background-color: %(editor.surface)s; }
	QFrame#OutlinePanel { border-right: 1px solid %(editor.border.soft)s; }
	QFrame#AssistPanel { border-left: 1px solid %(editor.border.soft)s; }
	QLabel#PanelHeaderTitle { font-size: 13px; font-weight: 800; color: %(editor.text)s; }
	QLabel#PanelHint { font-size: 11px; color: %(editor.hint)s; }
	QLabel#PanelEmpty { font-size: 12px; color: %(editor.empty)s; padding: 18px; }
	QPushButton#PanelHeaderClose {
		background-color: transparent; color: %(editor.text.secondary)s; border: none; border-radius: 6px; font-weight: 700;
	}
	QPushButton#PanelHeaderClose:hover { background-color: %(editor.border.soft)s; color: %(editor.text)s; }
	QListWidget#OutlineList, QListWidget#HistoryList {
		background-color: %(editor.surface)s; border: 1px solid %(editor.border.soft)s; border-radius: 8px; padding: 4px;
	}
	QListWidget#OutlineList::item, QListWidget#HistoryList::item { padding: 6px 8px; border-radius: 6px; color: %(editor.text.tertiary)s; }
	QListWidget#OutlineList::item:hover, QListWidget#HistoryList::item:hover { background-color: %(editor.hover)s; }
	QListWidget#OutlineList::item:selected, QListWidget#HistoryList::item:selected { background-color: %(editor.accent.subtle)s; color: %(editor.accent)s; }
	QTabWidget#AssistTabs::pane { border: 1px solid %(editor.border.soft)s; border-radius: 8px; top: -1px; }
	QTabBar::tab {
		background-color: %(editor.hover)s; color: %(editor.text.secondary)s; padding: 6px 14px; border-top-left-radius: 8px;
		border-top-right-radius: 8px; font-weight: 600; margin-right: 2px;
	}
	QTabBar::tab:selected { background-color: %(editor.surface)s; color: %(editor.accent)s; border: 1px solid %(editor.border.soft)s; border-bottom: none; }
	QPushButton#QuickActionButton {
		background-color: %(editor.surface)s; color: %(editor.text.tertiary)s; border: 1px solid %(editor.border)s; border-radius: 8px;
		padding: 9px 12px; font-weight: 600; text-align: left;
	}
	QPushButton#QuickActionButton:hover { background-color: %(editor.bar)s; border-color: %(editor.accent)s; color: %(editor.accent)s; }
	QTextEdit#AssistResult {
		background-color: %(editor.bar)s; color: %(editor.text)s; border: 1px solid %(editor.border.soft)s; border-radius: 8px; padding: 10px;
	}
	QTextEdit#AssistChatInput {
		background-color: %(editor.bar)s; border: 1px solid %(editor.border)s; border-radius: 10px; padding: 8px 10px; color: %(editor.text)s;
	}
	QTextEdit#AssistChatInput:focus { background-color: %(editor.surface)s; border-color: %(editor.accent)s; }
	QPushButton#PrimaryButton {
		background-color: %(editor.accent)s; color: %(text.on_accent)s; border: none; border-radius: 8px; padding: 8px 14px; font-weight: 700;
	}
	QPushButton#PrimaryButton:hover { background-color: %(editor.accent.hover)s; }
	QPushButton#GhostButton {
		background-color: %(editor.surface)s; color: %(editor.text.tertiary)s; border: 1px solid %(editor.border)s; border-radius: 8px; padding: 8px 12px; font-weight: 600;
	}
	QPushButton#GhostButton:hover { background-color: %(editor.hover)s; }
	QFrame#GhostChip { background-color: %(editor.chip.bg)s; border-radius: 8px; }
	QPushButton#GhostChipButton {
		background-color: transparent; color: %(editor.chip.text)s; border: none; padding: 2px 6px; font-size: 11px; font-weight: 600;
	}
	QPushButton#GhostChipButton:hover { color: %(text.on_accent)s; }
	QFrame#GhostGenChip { background-color: %(editor.genchip.bg)s; border: 1px solid %(editor.genchip.border)s; border-radius: 10px; }
	QLabel#GhostGenLabel { color: %(editor.text.secondary)s; font-size: 11px; font-weight: 600; background: transparent; }
	QFrame#EditorStatusBar {
		background-color: %(editor.bar)s; border-top: 1px solid %(editor.border.soft)s;
		border-bottom-left-radius: 12px; border-bottom-right-radius: 12px;
	}
	QLabel#EditorStatusItem { font-size: 11px; font-weight: 600; color: %(editor.text.secondary)s; }
	QMenu { background-color: %(editor.surface)s; border: 1px solid %(editor.border)s; border-radius: 8px; padding: 6px; }
	QMenu::item { color: %(editor.text)s; padding: 7px 26px 7px 12px; border-radius: 6px; }
	QMenu::item:selected { background-color: %(editor.accent.subtle)s; color: %(editor.accent)s; }
	QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
	QScrollBar::handle:vertical { background: %(editor.scrollbar)s; border-radius: 4px; min-height: 28px; }
	QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
	QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""


# --------------------------------------------------------------------------
# Floating assist window. Its QWidget rule deliberately sets NO background so
# the translucent rounded panel keeps working.
# --------------------------------------------------------------------------
_ASSIST_TMPL = """
	QWidget {
		color: %(text.primary2)s;
		font-family: """ + FONT_STACK + """;
		font-size: 13px;
	}
	QFrame#AssistPanel {
		background-color: %(surface.muted)s;
		border: 1px solid %(border.gray)s;
		border-radius: 16px;
	}
	QFrame#AssistPanel[maximized="true"] { border-radius: 0px; }
	QFrame#AssistContent[maximized="true"] {
		border-bottom-left-radius: 0px;
		border-bottom-right-radius: 0px;
	}
	QFrame#AssistTitleBar {
		background-color: %(surface)s;
		border-top-left-radius: 16px;
		border-top-right-radius: 16px;
		border-bottom: 1px solid %(border.gray)s;
	}
	QFrame#AssistContent {
		background-color: %(surface.muted)s;
		border-bottom-left-radius: 16px;
		border-bottom-right-radius: 16px;
	}
	QStackedWidget#AssistBodyStack {
		background-color: transparent;
	}
	QFrame#AssistViewToggle {
		background-color: %(viewtoggle.bg)s;
		border: 1px solid %(border)s;
		border-radius: 13px;
	}
	QPushButton#AssistViewSegment {
		background-color: transparent;
		color: %(text.secondary)s;
		border: 1px solid transparent;
		border-radius: 10px;
		padding: 8px 10px;
		font-size: 13px;
		font-weight: 800;
	}
	QPushButton#AssistViewSegment:hover {
		color: %(text.strong)s;
	}
	QPushButton#AssistViewSegment:checked {
		background-color: %(surface)s;
		color: %(info.fg)s;
		border: 1px solid %(viewtoggle.checked.border)s;
	}
	QPushButton#AssistViewSegment:checked:hover {
		color: %(info.fg)s;
	}
	QLabel#AssistWindowTitle {
		color: %(text.primary2)s;
		font-size: 13px;
		font-weight: 850;
	}
	QLabel#AssistTitleContext {
		color: %(text.secondary.gray)s;
		font-size: 11px;
		font-weight: 650;
	}
	QPushButton#AssistMinimizeButton,
	QPushButton#AssistCloseButton {
		background-color: transparent;
		color: %(text.secondary.gray)s;
		border: none;
		border-radius: 9px;
		font-size: 16px;
		font-weight: 700;
		padding: 0px;
	}
	QPushButton#AssistMinimizeButton:hover {
		background-color: %(surface.muted2)s;
		color: %(text.primary2)s;
	}
	QPushButton#AssistCloseButton:hover {
		background-color: %(danger.bg2)s;
		color: %(danger.fg2)s;
	}
	QFrame#AssistSectionCard {
		background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %(chat.surface.start)s, stop:1 %(chat.surface.end)s);
		border: 1px solid %(border.gray)s;
		border-radius: 13px;
	}
	QLabel#AssistSubText {
		color: %(text.secondary.gray)s;
		font-size: 12px;
		font-weight: 600;
	}
	QLabel#AssistSectionTitle {
		color: %(text.primary2)s;
		font-size: 13px;
		font-weight: 850;
	}
	QScrollArea#AssistScrollArea {
		background-color: transparent;
		border: none;
	}
	QFrame#SuggestionCard {
		background-color: %(surface)s;
		border: 1px solid %(border.gray)s;
		border-radius: 12px;
	}
	QLabel#SuggestionText {
		color: %(text.body)s;
		font-size: 13px;
		font-weight: 650;
		line-height: 1.5;
	}
	QLabel#AssistEmptyState {
		background-color: %(surface.muted)s;
		border: 1px dashed %(border.strong)s;
		border-radius: 12px;
		color: %(text.secondary.gray)s;
		padding: 18px 14px;
		font-weight: 650;
	}
	QPushButton#AssistCopyButton {
		background-color: %(surface)s;
		color: %(text.subtle)s;
		border: 1px solid %(border.gray.strong)s;
		border-radius: 8px;
		padding: 5px 8px;
		font-size: 11px;
		font-weight: 800;
	}
	QPushButton#AssistCopyButton:hover {
		background-color: %(surface.muted2)s;
		color: %(text.primary2)s;
	}
	QFrame#AssistUserBubble {
		background-color: %(bubble.user.bg)s;
		border: 1px solid %(bubble.user.border)s;
		border-radius: 13px;
		border-top-right-radius: 4px;
	}
	QFrame#AssistAiBubble {
		background-color: %(bubble.ai.bg)s;
		border: 1px solid %(bubble.ai.border)s;
		border-radius: 13px;
		border-top-left-radius: 4px;
	}
	QLabel#AssistBubbleMeta {
		color: %(text.secondary.gray)s;
		font-size: 11px;
		font-weight: 800;
	}
	QTextBrowser#AssistBubbleText {
		color: %(text.body)s;
		font-size: 14px;
		font-weight: 600;
		background: transparent;
		border: none;
	}
	QFrame#AssistInputBar {
		background-color: %(surface)s;
		border: 1px solid %(border.gray)s;
		border-radius: 14px;
	}
	QTextEdit#AssistChatInput {
		background-color: %(surface.muted)s;
		border: 1px solid %(border.gray)s;
		border-radius: 11px;
		padding: 8px 10px;
		color: %(text.primary2)s;
		font-size: 13px;
		selection-background-color: %(selection.bg)s;
		selection-color: %(text.primary2)s;
	}
	QTextEdit#AssistChatInput:focus {
		background-color: %(surface)s;
		border: 1px solid %(blue)s;
	}
	QPushButton#AssistSendButton {
		background-color: %(send.flat.bg)s;
		border: 1px solid %(send.flat.border)s;
		border-radius: 11px;
		color: %(text.on_accent)s;
		font-weight: 850;
	}
	QPushButton#AssistSendButton:hover {
		background-color: %(blue.hover)s;
	}
	QPushButton#AssistModeButton {
		background-color: %(surface.inset)s;
		color: %(text.slate600)s;
		border: 1px solid %(border.gray.strong)s;
		border-radius: 11px;
		padding: 0px;
		font-size: 13px;
		font-weight: 800;
	}
	QPushButton#AssistModeButton:hover {
		background-color: %(accent.subtle.bg.hover)s;
		border-color: %(accent.border.checked)s;
		color: %(accent.text)s;
	}
	QPushButton#AssistModeButton[researchActive="true"] {
		background-color: %(deepblue.start)s;
		border-color: %(deepblue.start)s;
		color: %(text.on_accent)s;
	}
	QPushButton#AssistModeButton[researchActive="true"]:hover {
		background-color: %(deepblue.end)s;
		border-color: %(deepblue.end)s;
		color: %(text.on_accent)s;
	}
	QMenu {
		background-color: %(menu.bg)s;
		border: 1px solid %(menu.border)s;
		border-radius: 8px;
		padding: 6px;
	}
	QMenu::item {
		color: %(menu.item.text)s;
		padding: 8px 28px 8px 12px;
		border-radius: 6px;
	}
	QMenu::item:selected {
		background-color: %(menu.item.sel.bg)s;
		color: %(menu.item.sel.text)s;
	}
	QScrollBar:vertical {
		background: transparent;
		width: 8px;
		margin: 2px 0 2px 0;
	}
	QScrollBar::handle:vertical {
		background: %(scrollbar.handle)s;
		border-radius: 4px;
		min-height: 26px;
	}
	QScrollBar::add-line:vertical,
	QScrollBar::sub-line:vertical {
		height: 0px;
	}
	QScrollBar::add-page:vertical,
	QScrollBar::sub-page:vertical {
		background: transparent;
	}
"""


def build_main_window_qss(p: dict[str, str]) -> str:
	return (_MAIN_TMPL % p) + titlebar_qss(p)


def build_editor_qss(p: dict[str, str]) -> str:
	return (_EDITOR_TMPL % p) + titlebar_qss(p) + chat_qss(p)


def build_assist_window_qss(p: dict[str, str]) -> str:
	return (_ASSIST_TMPL % p) + titlebar_qss(p) + chat_qss(p)
