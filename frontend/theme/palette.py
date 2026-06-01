"""Semantic colour tokens for the VERITAS frontend.

A single source of truth for every UI-chrome colour. Each token names a *role*
(``"surface"``, ``"text.primary"``, ``"accent"`` …) rather than a literal colour,
so the same stylesheet can be rendered light or dark by swapping the palette.

Design rule: the ``LIGHT`` palette reproduces the colours the app shipped with
*exactly*, so enabling the theme system changes nothing in light mode. ``DARK``
supplies a slate-based dark counterpart that keeps the app's existing
indigo/blue identity.

Semantic status colours (success / warning / danger / info) keep their meaning
in both modes — only their tint/contrast is adapted for a dark surface.

``rgba(...)`` strings are valid Qt stylesheet colours and are used where a
translucent overlay reads better than a flat tint (common in dark mode).
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# LIGHT — reproduces the previously hard-coded colours one-for-one.
# ---------------------------------------------------------------------------
LIGHT: dict[str, str] = {
	# -- app shell ---------------------------------------------------------
	"bg.base": "#F6F8FC",            # QWidget fallback background
	"bg.app.start": "#F8FAFC",       # AppRoot gradient top
	"bg.app.end": "#EEF2FF",         # AppRoot gradient bottom
	# -- surfaces ----------------------------------------------------------
	"surface": "#FFFFFF",            # cards, panels, popups
	"surface.muted": "#F8FAFC",      # inset fields, count cards, status pills
	"surface.inset": "#F1F5F9",      # mode buttons, code, deepest inset
	"surface.muted2": "#F3F4F6",     # neutral hover, idle badge
	# -- borders -----------------------------------------------------------
	"border": "#E2E8F0",             # default panel/card border (slate-200)
	"border.gray": "#E5E7EB",        # gray-200 border variant
	"border.strong": "#CBD5E1",      # input border (slate-300)
	"border.gray.strong": "#D1D5DB", # gray-300 border variant
	"border.hover": "#94A3B8",       # ghost/secondary hover border
	# -- text --------------------------------------------------------------
	"text.primary": "#0F172A",       # headings (slate-900)
	"text.primary2": "#111827",      # near-black body (gray-900)
	"text.body": "#1F2937",          # body copy (gray-800)
	"text.strong": "#334155",        # ghost-button label (slate-700)
	"text.secondary": "#64748B",     # secondary copy (slate-500)
	"text.secondary.gray": "#6B7280",  # secondary copy (gray-500)
	"text.muted": "#94A3B8",         # muted / footnote (slate-400)
	"text.muted.gray": "#9CA3AF",    # disabled (gray-400)
	"text.subtle": "#4B5563",        # copy-button label (gray-600)
	"text.on_accent": "#FFFFFF",     # text on accent/dark fills
	"text.on_dark": "#F8FAFC",       # text on the dark default button
	# -- default (dark) push button ---------------------------------------
	"button.bg": "#1F2937",
	"button.bg.hover": "#111827",
	# -- accent (indigo) ---------------------------------------------------
	"accent": "#4F46E5",
	"accent.hover": "#4338CA",
	"accent.text": "#3730A3",            # text/glyph on a subtle-accent fill
	"accent.subtle.bg": "#EEF2FF",       # indigo-50 chips / selected rows
	"accent.subtle.bg.hover": "#E0E7FF", # indigo-100 hover
	"accent.subtle.border": "#C7D2FE",   # indigo-200
	"accent.border.checked": "#818CF8",  # indigo-400 selected outline
	"accent.deep.text": "#312E81",       # indigo-900 on user bubble
	"accent.glyph": "#6366F1",           # indigo-500 small accents / meta
	# -- blue (send / inputs focus / links / research-active) --------------
	"blue": "#3B82F6",
	"blue.hover": "#2563EB",
	"link": "#2563EB",
	"focus.border": "#7C3AED",       # chat input focus (violet)
	"deepblue.start": "#1E3A8A",     # research-active / hero start
	"deepblue.end": "#1E40AF",
	# -- send-button violet gradient (write page) --------------------------
	"send.grad.start": "#7C3AED",
	"send.grad.end": "#5B21B6",
	"send.grad.start.hover": "#6D28D9",
	"send.grad.end.hover": "#4C1D95",
	"send.flat.bg": "#3B82F6",       # assist send button (flat)
	"send.flat.border": "#2563EB",
	# -- selection ---------------------------------------------------------
	"selection.bg": "#BFDBFE",
	"selection.text": "#0F172A",
	"selection.bg.violet": "#E9D5FF",
	"selection.bg.indigo": "#C7D2FE",
	# -- scrollbar ---------------------------------------------------------
	"scrollbar.handle": "#CBD5E1",
	"scrollbar.handle.hover": "#94A3B8",
	# -- sidebar (dark navy in light mode; stays dark in dark mode) --------
	"sidebar.bg": "#0F172A",
	"sidebar.border": "#1E293B",
	"sidebar.brand": "#FFFFFF",
	"sidebar.brand.sub": "#94A3B8",
	"sidebar.footer.bg": "rgba(99, 102, 241, 0.18)",
	"sidebar.footer.border": "rgba(165, 180, 252, 0.55)",
	"sidebar.footer.title": "#C7D2FE",
	"sidebar.footer.desc": "#F8FAFC",
	"sidebar.wsbtn.bg": "rgba(255, 255, 255, 0.12)",
	"sidebar.wsbtn.bg.hover": "rgba(255, 255, 255, 0.20)",
	"sidebar.wsbtn.text": "#E2E8F0",
	"sidebar.wsbtn.border": "rgba(148, 163, 184, 0.45)",
	"sidebar.wsbtn.border.hover": "rgba(148, 163, 184, 0.75)",
	"sidebar.nav.text": "#D6DBE5",
	"sidebar.nav.hover": "rgba(255, 255, 255, 18)",
	"sidebar.nav.checked.bg": "rgba(99, 102, 241, 48)",
	"sidebar.nav.checked.border": "rgba(165, 180, 252, 148)",
	"sidebar.nav.checked.text": "#F8FAFC",
	"sidebar.chevron": "#FFFFFF",
	# -- hero band ---------------------------------------------------------
	"hero.start": "#1E3A8A",
	"hero.end": "#3730A3",
	"hero.border": "rgba(129, 140, 248, 0.52)",
	"hero.title": "#FFFFFF",
	"hero.desc": "#DDE7FF",
	"hero.chip.bg": "rgba(255, 255, 255, 0.16)",
	"hero.chip.border": "rgba(255, 255, 255, 0.24)",
	"hero.chip.text": "#F8FAFC",
	"hero.btn.bg": "rgba(255, 255, 255, 0.14)",
	"hero.btn.bg.hover": "rgba(255, 255, 255, 0.22)",
	"hero.btn.border": "rgba(255, 255, 255, 0.30)",
	"hero.btn.text": "#FFFFFF",
	# -- chat hero (write page) -------------------------------------------
	"chathero.start": "#1E293B",
	"chathero.end": "#334155",
	"chathero.border": "#475569",
	"chathero.iconbox.bg": "rgba(255, 255, 255, 0.12)",
	"chathero.iconbox.border": "rgba(255, 255, 255, 0.20)",
	# -- chat surfaces (shared by assist window + chat pages) --------------
	"chat.surface.start": "#E3EFFB",
	"chat.surface.end": "#F4F9FE",
	"bubble.user.bg": "#DBEAFE",
	"bubble.user.border": "#BFDBFE",
	"bubble.ai.bg": "#FFFFFF",
	"bubble.ai.border": "#E5E7EB",
	# write-page bubbles (indigo user / slate ai)
	"bubble2.user.bg": "#EEF2FF",
	"bubble2.user.border": "#C7D2FE",
	"bubble2.user.text": "#312E81",
	"bubble2.user.meta": "#6366F1",
	"bubble2.ai.bg": "#F8FAFC",
	"bubble2.ai.border": "#E2E8F0",
	"bubble2.text": "#1F2937",
	"bubble2.meta": "#9CA3AF",
	# -- editor chrome (Google-Docs-like grays) ---------------------------
	"editor.text": "#202124",
	"editor.text.secondary": "#5F6368",
	"editor.text.tertiary": "#3C4043",
	"editor.surface": "#FFFFFF",
	"editor.bar": "#F8F9FA",
	"editor.canvas": "#F6F7F9",
	"editor.border": "#DADCE0",
	"editor.border.soft": "#E8EAED",
	"editor.border.softer": "#F1F3F4",
	"editor.hover": "#F1F3F4",
	"editor.accent": "#0B57D0",
	"editor.accent.hover": "#1967D2",
	"editor.accent.subtle": "#E8F0FE",
	"editor.selection": "#D3E3FD",
	"editor.close.hover.bg": "#FCE8E6",
	"editor.close.hover.fg": "#D93025",
	"editor.scrollbar": "#BDC1C6",
	"editor.hint": "#80868B",
	"editor.empty": "#9AA0A6",
	"editor.ghost": "#9AA0A6",        # ghost-text autocomplete
	"editor.chip.bg": "#202124",
	"editor.chip.text": "#E8EAED",
	"editor.genchip.bg": "#F1F3F4",
	"editor.genchip.border": "#E0E3E7",
	# -- shared frameless title bar ---------------------------------------
	"titlebar.bg": "#FFFFFF",
	"titlebar.border": "#E5E7EB",
	"titlebar.brand": "#111827",
	"titlebar.sub": "#6B7280",
	"titlebar.sep": "#CBD5E1",
	"titlebar.winctl.hover": "#EDEFF2",
	"titlebar.close.hover": "#E81123",
	"titlebar.winctl.glyph": "#3C4043",
	"titlebar.winctl.glyph.on": "#FFFFFF",
	# -- menus -------------------------------------------------------------
	"menu.bg": "#FFFFFF",
	"menu.border": "#CBD5E1",
	"menu.item.text": "#111827",
	"menu.item.sel.bg": "#EEF2FF",
	"menu.item.sel.text": "#3730A3",
	# -- misc panel labels -------------------------------------------------
	"text.slate600": "#475569",      # AssistModeButton / SettingsStatus label
	"viewtoggle.bg": "#EEF1F6",      # assist view segmented control track
	"viewtoggle.checked.border": "#DCE3EC",
	"stat.delta": "#10B981",         # success green for stat deltas
	"shadow": "rgba(2, 6, 23, 0.06)",
	# -- semantic status: success -----------------------------------------
	"success.bg": "#ECFDF3",
	"success.fg": "#15803D",
	"success.border": "#BBF7D0",
	"success.strong": "#047857",
	# -- semantic status: warning -----------------------------------------
	"warning.bg": "#FFF7ED",
	"warning.fg": "#9A3412",
	"warning.fg2": "#B45309",
	"warning.fg3": "#92400E",
	"warning.border": "#FED7AA",
	# -- semantic status: danger ------------------------------------------
	"danger.bg": "#FEF2F2",
	"danger.fg": "#B91C1C",
	"danger.fg2": "#DC2626",
	"danger.fg.strong": "#991B1B",
	"danger.border": "#FECACA",
	"danger.border2": "#FCA5A5",
	"danger.bg2": "#FEE2E2",
	# -- semantic status: info (indigo/blue) ------------------------------
	"info.bg": "#DBEAFE",
	"info.fg": "#1D4ED8",
	"info.border": "#BFDBFE",
	# -- workflow stepper (amber/tan) -------------------------------------
	"stepper.active.bg": "#F2DDC0",
	"stepper.active.border": "#D8A467",
	"stepper.active.text": "#B96016",
	"stepper.done.bg": "#F4E4CC",
	"stepper.done.border": "#D8A467",
	"stepper.done.text": "#A85A16",
	"stepper.pending.bg": "#F8FAFC",
	"stepper.pending.border": "#E5E7EB",
	"stepper.pending.text": "#94A3B8",
	"stepper.connector.on": "#D8A467",
	"stepper.connector.off": "#EAD5B8",
	# -- progress bar (research) ------------------------------------------
	"progress.track": "#E8EDF4",
	"progress.running.start": "#6366F1",
	"progress.running.end": "#3B82F6",
	"progress.completed.start": "#34D399",
	"progress.completed.end": "#10B981",
	"progress.partial.start": "#FBBF24",
	"progress.partial.end": "#F59E0B",
	"progress.failed.start": "#F87171",
	"progress.failed.end": "#EF4444",
	# -- assist status badge ----------------------------------------------
	"badge.working.bg": "#DBEAFE",
	"badge.working.fg": "#1D4ED8",
	"badge.working.border": "#BFDBFE",
	"badge.idle.bg": "#F3F4F6",
	"badge.idle.fg": "#6B7280",
	"badge.idle.border": "#E5E7EB",
	"badge.warning.bg": "#FEF3C7",
	"badge.warning.fg": "#B45309",
	"badge.warning.border": "#FDE68A",
	"badge.error.bg": "#FEE2E2",
	"badge.error.fg": "#DC2626",
	"badge.error.border": "#FECACA",
	# -- rating chips (assist suggestion) ---------------------------------
	"chip.like.hover.bg": "#ECFDF5",
	"chip.like.hover.fg": "#047857",
	"chip.like.hover.border": "#6EE7B7",
	"chip.like.chosen.bg": "#D1FAE5",
	"chip.dislike.hover.bg": "#FEF2F2",
	"chip.dislike.hover.fg": "#B91C1C",
	"chip.dislike.hover.border": "#FCA5A5",
	"chip.dislike.chosen.bg": "#FEE2E2",
	"chip.base.bg": "#FFFFFF",
	"chip.base.fg": "#4B5563",
	"chip.base.border": "#D1D5DB",
	# -- dashboard action buttons (semantic) ------------------------------
	"action.edit.fg": "#047857",
	"action.edit.border": "#6EE7B7",
	"action.edit.hover.bg": "#D1FAE5",
	"action.edit.hover.border": "#34D399",
	"action.delete.fg": "#B91C1C",
	"action.delete.border": "#FCA5A5",
	"action.delete.hover.bg": "#FEE2E2",
	"action.delete.hover.border": "#F87171",
	"action.open.fg": "#1D4ED8",
	"action.open.border": "#93C5FD",
	"action.open.hover.bg": "#EFF6FF",
	"action.open.hover.border": "#60A5FA",
	# -- verify reliability-band left accent (saturated, reads on any bg) --
	"verify.band.high": "#22C55E",
	"verify.band.medium": "#F59E0B",
	"verify.band.low": "#EF4444",
	# -- markdown rendered content ----------------------------------------
	"md.text": "#1F2937",
	"md.heading": "#0F172A",
	"md.code.bg": "#F1F5F9",
	"md.code.text": "#0F172A",
	"md.pre.border": "#E2E8F0",
	"md.quote.border": "#C7D2FE",
	"md.quote.text": "#4B5563",
	"md.table.border": "#CBD5E1",
	"md.th.bg": "#F1F5F9",
	"md.tr.even.bg": "#F8FAFC",
	"md.link": "#2563EB",
	"md.hr": "#E2E8F0",
}


# ---------------------------------------------------------------------------
# DARK — slate-based counterpart that preserves the indigo/blue identity.
# ---------------------------------------------------------------------------
DARK: dict[str, str] = {
	# -- app shell ---------------------------------------------------------
	"bg.base": "#0B1120",
	"bg.app.start": "#0B1120",
	"bg.app.end": "#0F172A",
	# -- surfaces ----------------------------------------------------------
	"surface": "#1E293B",
	"surface.muted": "#172132",
	"surface.inset": "#131C2B",
	"surface.muted2": "#212E42",
	# -- borders -----------------------------------------------------------
	"border": "#334155",
	"border.gray": "#2C3A4F",
	"border.strong": "#3E4D63",
	"border.gray.strong": "#3A4860",
	"border.hover": "#64748B",
	# -- text --------------------------------------------------------------
	"text.primary": "#F1F5F9",
	"text.primary2": "#F3F5F9",
	"text.body": "#E2E8F0",
	"text.strong": "#CBD5E1",
	"text.secondary": "#94A3B8",
	"text.secondary.gray": "#9BA6B6",
	"text.muted": "#7E8CA0",
	"text.muted.gray": "#6B7888",
	"text.subtle": "#AEB8C7",
	"text.on_accent": "#FFFFFF",
	"text.on_dark": "#F8FAFC",
	# -- default push button (lighter slate so it reads on dark) -----------
	"button.bg": "#334155",
	"button.bg.hover": "#3E4D63",
	# -- accent (indigo, brightened for dark) ------------------------------
	"accent": "#6366F1",
	"accent.hover": "#818CF8",
	"accent.text": "#C7D2FE",
	"accent.subtle.bg": "rgba(99, 102, 241, 0.16)",
	"accent.subtle.bg.hover": "rgba(99, 102, 241, 0.26)",
	"accent.subtle.border": "rgba(129, 140, 248, 0.45)",
	"accent.border.checked": "#6366F1",
	"accent.deep.text": "#C7D2FE",
	"accent.glyph": "#818CF8",
	# -- blue --------------------------------------------------------------
	"blue": "#3B82F6",
	"blue.hover": "#60A5FA",
	"link": "#60A5FA",
	"focus.border": "#A78BFA",
	"deepblue.start": "#3730A3",
	"deepblue.end": "#4338CA",
	# -- send-button violet gradient ---------------------------------------
	"send.grad.start": "#7C3AED",
	"send.grad.end": "#5B21B6",
	"send.grad.start.hover": "#8B5CF6",
	"send.grad.end.hover": "#6D28D9",
	"send.flat.bg": "#3B82F6",
	"send.flat.border": "#2563EB",
	# -- selection ---------------------------------------------------------
	"selection.bg": "#1D4ED8",
	"selection.text": "#F1F5F9",
	"selection.bg.violet": "#5B21B6",
	"selection.bg.indigo": "#3730A3",
	# -- scrollbar ---------------------------------------------------------
	"scrollbar.handle": "#3E4D63",
	"scrollbar.handle.hover": "#566378",
	# -- sidebar (deepest panel) ------------------------------------------
	"sidebar.bg": "#070C16",
	"sidebar.border": "#1E293B",
	"sidebar.brand": "#FFFFFF",
	"sidebar.brand.sub": "#94A3B8",
	"sidebar.footer.bg": "rgba(99, 102, 241, 0.22)",
	"sidebar.footer.border": "rgba(129, 140, 248, 0.45)",
	"sidebar.footer.title": "#C7D2FE",
	"sidebar.footer.desc": "#F8FAFC",
	"sidebar.wsbtn.bg": "rgba(255, 255, 255, 0.08)",
	"sidebar.wsbtn.bg.hover": "rgba(255, 255, 255, 0.16)",
	"sidebar.wsbtn.text": "#E2E8F0",
	"sidebar.wsbtn.border": "rgba(148, 163, 184, 0.35)",
	"sidebar.wsbtn.border.hover": "rgba(148, 163, 184, 0.6)",
	"sidebar.nav.text": "#C2CAD8",
	"sidebar.nav.hover": "rgba(255, 255, 255, 14)",
	"sidebar.nav.checked.bg": "rgba(99, 102, 241, 60)",
	"sidebar.nav.checked.border": "rgba(165, 180, 252, 130)",
	"sidebar.nav.checked.text": "#F8FAFC",
	"sidebar.chevron": "#FFFFFF",
	# -- hero band ---------------------------------------------------------
	"hero.start": "#312E81",
	"hero.end": "#1E1B4B",
	"hero.border": "rgba(129, 140, 248, 0.45)",
	"hero.title": "#FFFFFF",
	"hero.desc": "#C7D2FE",
	"hero.chip.bg": "rgba(255, 255, 255, 0.12)",
	"hero.chip.border": "rgba(255, 255, 255, 0.20)",
	"hero.chip.text": "#F8FAFC",
	"hero.btn.bg": "rgba(255, 255, 255, 0.12)",
	"hero.btn.bg.hover": "rgba(255, 255, 255, 0.20)",
	"hero.btn.border": "rgba(255, 255, 255, 0.26)",
	"hero.btn.text": "#FFFFFF",
	# -- chat hero ---------------------------------------------------------
	"chathero.start": "#0F172A",
	"chathero.end": "#1E293B",
	"chathero.border": "#334155",
	"chathero.iconbox.bg": "rgba(255, 255, 255, 0.10)",
	"chathero.iconbox.border": "rgba(255, 255, 255, 0.18)",
	# -- chat surfaces -----------------------------------------------------
	"chat.surface.start": "#172132",
	"chat.surface.end": "#131C2B",
	"bubble.user.bg": "#1E3A5F",
	"bubble.user.border": "#2C4A73",
	"bubble.ai.bg": "#1E293B",
	"bubble.ai.border": "#334155",
	"bubble2.user.bg": "#312E81",
	"bubble2.user.border": "#4338CA",
	"bubble2.user.text": "#E0E7FF",
	"bubble2.user.meta": "#A5B4FC",
	"bubble2.ai.bg": "#172132",
	"bubble2.ai.border": "#334155",
	"bubble2.text": "#E2E8F0",
	"bubble2.meta": "#7E8CA0",
	# -- editor chrome (dark IDE-like) ------------------------------------
	"editor.text": "#E3E5E8",
	"editor.text.secondary": "#9AA0A6",
	"editor.text.tertiary": "#C4C7CC",
	"editor.surface": "#1E2230",
	"editor.bar": "#181C28",
	"editor.canvas": "#11141C",
	"editor.border": "#2C3340",
	"editor.border.soft": "#2A303C",
	"editor.border.softer": "#242A36",
	"editor.hover": "#2A313F",
	"editor.accent": "#8AB4F8",
	"editor.accent.hover": "#AECBFA",
	"editor.accent.subtle": "#1F3354",
	"editor.selection": "#264F78",
	"editor.close.hover.bg": "#5C1A1A",
	"editor.close.hover.fg": "#F2837F",
	"editor.scrollbar": "#3E4654",
	"editor.hint": "#8A9099",
	"editor.empty": "#7A828C",
	"editor.ghost": "#6E7681",
	"editor.chip.bg": "#0B0E14",
	"editor.chip.text": "#C4C7CC",
	"editor.genchip.bg": "#242A36",
	"editor.genchip.border": "#2C3340",
	# -- shared title bar --------------------------------------------------
	"titlebar.bg": "#1E2230",
	"titlebar.border": "#2C3340",
	"titlebar.brand": "#F1F5F9",
	"titlebar.sub": "#9BA6B6",
	"titlebar.sep": "#3E4D63",
	"titlebar.winctl.hover": "#2A313F",
	"titlebar.close.hover": "#C42B1C",
	"titlebar.winctl.glyph": "#C4C7CC",
	"titlebar.winctl.glyph.on": "#FFFFFF",
	# -- menus -------------------------------------------------------------
	"menu.bg": "#1E293B",
	"menu.border": "#3E4D63",
	"menu.item.text": "#E2E8F0",
	"menu.item.sel.bg": "rgba(99, 102, 241, 0.22)",
	"menu.item.sel.text": "#C7D2FE",
	# -- misc --------------------------------------------------------------
	"text.slate600": "#AEB8C7",
	"viewtoggle.bg": "#172132",
	"viewtoggle.checked.border": "#334155",
	"stat.delta": "#34D399",
	"shadow": "rgba(0, 0, 0, 0.45)",
	# -- semantic status: success -----------------------------------------
	"success.bg": "rgba(34, 197, 94, 0.16)",
	"success.fg": "#6EE7B7",
	"success.border": "rgba(52, 211, 153, 0.45)",
	"success.strong": "#6EE7B7",
	# -- semantic status: warning -----------------------------------------
	"warning.bg": "rgba(245, 158, 11, 0.16)",
	"warning.fg": "#FCD34D",
	"warning.fg2": "#FBBF24",
	"warning.fg3": "#FCD34D",
	"warning.border": "rgba(251, 191, 36, 0.45)",
	# -- semantic status: danger ------------------------------------------
	"danger.bg": "rgba(239, 68, 68, 0.16)",
	"danger.fg": "#FCA5A5",
	"danger.fg2": "#F87171",
	"danger.fg.strong": "#FCA5A5",
	"danger.border": "rgba(248, 113, 113, 0.45)",
	"danger.border2": "rgba(248, 113, 113, 0.55)",
	"danger.bg2": "rgba(239, 68, 68, 0.22)",
	# -- semantic status: info --------------------------------------------
	"info.bg": "rgba(59, 130, 246, 0.18)",
	"info.fg": "#93C5FD",
	"info.border": "rgba(96, 165, 250, 0.45)",
	# -- workflow stepper --------------------------------------------------
	"stepper.active.bg": "rgba(216, 164, 103, 0.30)",
	"stepper.active.border": "#D8A467",
	"stepper.active.text": "#F0B878",
	"stepper.done.bg": "rgba(216, 164, 103, 0.18)",
	"stepper.done.border": "#A8743E",
	"stepper.done.text": "#E0A468",
	"stepper.pending.bg": "#172132",
	"stepper.pending.border": "#334155",
	"stepper.pending.text": "#7E8CA0",
	"stepper.connector.on": "#D8A467",
	"stepper.connector.off": "#4A4032",
	# -- progress bar ------------------------------------------------------
	"progress.track": "#26334A",
	"progress.running.start": "#6366F1",
	"progress.running.end": "#3B82F6",
	"progress.completed.start": "#34D399",
	"progress.completed.end": "#10B981",
	"progress.partial.start": "#FBBF24",
	"progress.partial.end": "#F59E0B",
	"progress.failed.start": "#F87171",
	"progress.failed.end": "#EF4444",
	# -- assist status badge ----------------------------------------------
	"badge.working.bg": "rgba(59, 130, 246, 0.20)",
	"badge.working.fg": "#93C5FD",
	"badge.working.border": "rgba(96, 165, 250, 0.45)",
	"badge.idle.bg": "#212E42",
	"badge.idle.fg": "#9BA6B6",
	"badge.idle.border": "#334155",
	"badge.warning.bg": "rgba(245, 158, 11, 0.16)",
	"badge.warning.fg": "#FCD34D",
	"badge.warning.border": "rgba(251, 191, 36, 0.45)",
	"badge.error.bg": "rgba(239, 68, 68, 0.16)",
	"badge.error.fg": "#FCA5A5",
	"badge.error.border": "rgba(248, 113, 113, 0.45)",
	# -- rating chips ------------------------------------------------------
	"chip.like.hover.bg": "rgba(16, 185, 129, 0.20)",
	"chip.like.hover.fg": "#6EE7B7",
	"chip.like.hover.border": "rgba(110, 231, 183, 0.5)",
	"chip.like.chosen.bg": "rgba(16, 185, 129, 0.28)",
	"chip.dislike.hover.bg": "rgba(239, 68, 68, 0.20)",
	"chip.dislike.hover.fg": "#FCA5A5",
	"chip.dislike.hover.border": "rgba(248, 113, 113, 0.5)",
	"chip.dislike.chosen.bg": "rgba(239, 68, 68, 0.28)",
	"chip.base.bg": "#212E42",
	"chip.base.fg": "#CBD5E1",
	"chip.base.border": "#3E4D63",
	# -- dashboard action buttons -----------------------------------------
	"action.edit.fg": "#6EE7B7",
	"action.edit.border": "rgba(110, 231, 183, 0.45)",
	"action.edit.hover.bg": "rgba(16, 185, 129, 0.20)",
	"action.edit.hover.border": "#34D399",
	"action.delete.fg": "#FCA5A5",
	"action.delete.border": "rgba(248, 113, 113, 0.45)",
	"action.delete.hover.bg": "rgba(239, 68, 68, 0.20)",
	"action.delete.hover.border": "#F87171",
	"action.open.fg": "#93C5FD",
	"action.open.border": "rgba(96, 165, 250, 0.45)",
	"action.open.hover.bg": "rgba(59, 130, 246, 0.18)",
	"action.open.hover.border": "#60A5FA",
	# -- verify reliability-band left accent -------------------------------
	"verify.band.high": "#34D399",
	"verify.band.medium": "#FBBF24",
	"verify.band.low": "#F87171",
	# -- markdown rendered content ----------------------------------------
	"md.text": "#D7DEE8",
	"md.heading": "#F1F5F9",
	"md.code.bg": "#131C2B",
	"md.code.text": "#E2E8F0",
	"md.pre.border": "#334155",
	"md.quote.border": "#4338CA",
	"md.quote.text": "#AEB8C7",
	"md.table.border": "#3E4D63",
	"md.th.bg": "#212E42",
	"md.tr.even.bg": "#172132",
	"md.link": "#93C5FD",
	"md.hr": "#334155",
}


PALETTES: dict[str, dict[str, str]] = {"light": LIGHT, "dark": DARK}


def _assert_parity() -> None:
	"""Guard against a token defined in one palette but not the other."""
	missing_in_dark = set(LIGHT) - set(DARK)
	missing_in_light = set(DARK) - set(LIGHT)
	if missing_in_dark or missing_in_light:
		raise AssertionError(
			"theme palette token mismatch — "
			f"missing in dark: {sorted(missing_in_dark)}; "
			f"missing in light: {sorted(missing_in_light)}"
		)


_assert_parity()
