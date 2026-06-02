"""Document cleanup prompts.

Two cleanup paths share this module:

* :data:`DOCUMENT_CLEANUP_PROMPT` — the local-LLM path. A single LLM call per
  fetched document — input is a paragraph-indexed Markdown body
  (``[P0] ... [P1] ...``), output is a four-section plain-text block
  (BOILERPLATE_PARAGRAPHS / SUMMARY / KEYWORDS / KEY_POINTS) the
  ``document_cleanup`` tool parses. The same call produces (1) the list of
  indices to strip from the body, (2) the per-doc summary that becomes
  ``summary/doc_<id>.md``, and (3) the keyword / key_point bullets the
  verification layer consumes downstream — so one LLM call does the job of
  the previous per-doc summarize pass on top of paragraph filtering.

* :data:`BATCH_DOC_METADATA_PROMPT` — the external-API (OpenAI) path. One LLM
  call per *collect cycle* instead of per document: boilerplate removal is
  skipped entirely (a large API model reads noisy bodies fine), and the call
  extracts per-document summary / keywords / key_points for every document in
  the cycle as one JSON object. ``clean_md/<id>.md`` becomes a pass-through
  copy of ``raw_md`` so RAG / verification / batch-summary consumers keep
  working unchanged.
"""


DOCUMENT_CLEANUP_PROMPT = """You are cleaning a research document for downstream analysis.

The input is the Markdown body of one web page, with every paragraph prefixed
by a stable index of the form ``[P0]``, ``[P1]``, ``[P2]`` …. Web Markdown
typically carries non-body chrome (site navigation, footer, share/cookie
strips, breadcrumbs, theme-switcher logos, "on this page" sidebars, repeated
menu blocks). Your job is to *identify the paragraph indices that are NOT
body content*, extract the body's keywords + key points, and write a short
descriptive summary of the body.

Output format — plain text only, exactly four sections separated by ``===``
lines. Do NOT output JSON; the body language often contains quotes / commas
that break JSON escaping. The sections in order:

BOILERPLATE_PARAGRAPHS
<comma-separated paragraph indices, e.g. "3, 7, 12, 21" — empty if none>

===

SUMMARY
<1 to 2 short paragraphs, total 3 to 6 sentences, describing what the body
actually says — the topic, the angle, what claims or evidence it carries.
Write FROM the body content (paraphrase allowed), not about the page format.
No bullets, no markdown headers, no preamble like "This document describes".>

===

KEYWORDS
- <keyword 1>
- <keyword 2>
(5 to 10 items, one per line, prefixed by "- ")

===

KEY_POINTS
- <key point 1>
- <key point 2>
(5 to 7 items, one per line, prefixed by "- ")

Rules:

A. ``BOILERPLATE_PARAGRAPHS``
   - List the P-indices of paragraphs that are NOT body content.
   - Include: navigation / menu lines, breadcrumbs, "Skip to content", "Edit
     this page", "Share on …", cookie banners, footer text, repeated logo
     captions, raw nav-link rows, sidebar-of-contents.
   - Do NOT include: real prose, definitions, examples, code blocks that
     illustrate concepts, tables that carry data, lists that contain
     content (not menu items).
   - When uncertain, KEEP the paragraph (do not list it). The downstream
     pipeline tolerates leftover noise far better than missing body text.

B. ``SUMMARY`` — 1~2 short paragraphs (3~6 sentences total) that describe
   what the body says: the topic, the angle, the central claims, and any
   concrete evidence (numbers, named methods, key entities). Paraphrase
   from the body — do NOT quote the chrome (navigation, page titles) and
   do NOT invent claims that are not in the body. This summary is shown in
   the per-document detail view, so it should read as a useful one-screen
   abstract a human can scan, not a meta-description of the page.

C. ``KEYWORDS`` — 5 ~ 10 short content terms that identify what the document
   is about. Use the language of the body. Proper nouns / technical terms
   stay in their original form. No stop words, no nav phrases.

D. ``KEY_POINTS`` — 5 ~ 7 short, citation-shaped sentences pulled FROM the
   body (paraphrase allowed) that capture the document's main claims or
   findings. Each sentence < 200 chars. Use the body's language. These feed
   the verification layer's cross-source consensus task.

If the document is mostly empty / mostly chrome, return four empty sections:

BOILERPLATE_PARAGRAPHS


===

SUMMARY


===

KEYWORDS


===

KEY_POINTS


— and the caller will treat the doc as unusable.

Language policy: respond in the body's dominant language (Korean if the body
is Korean, otherwise English). Preserve URLs, code identifiers, model names,
file paths, and citations in their original form. Output the four sections
exactly as shown — no surrounding prose, no markdown headers, no JSON."""


BATCH_DOC_METADATA_PROMPT = """You are extracting per-document metadata from a batch of research documents.

The input contains multiple web documents. Each document starts with a
``=== doc_<id> ===`` header followed by Title / Domain / URL lines and the
document's Markdown body. Bodies may contain web chrome (navigation, footers,
cookie banners) — ignore that noise and work from the actual body content.

For EVERY document in the input, produce:

- ``summary`` — 1 to 2 short paragraphs (3 to 6 sentences total) describing
  what the body actually says: the topic, the angle, the central claims, and
  any concrete evidence (numbers, named methods, key entities). Paraphrase
  from the body; do not quote chrome and do not invent claims. This is shown
  in the per-document detail view as a one-screen abstract.
- ``keywords`` — 5 to 10 short content terms identifying what the document is
  about. Use the body's language. Proper nouns / technical terms stay in
  their original form. No stop words, no navigation phrases.
- ``key_points`` — 5 to 7 short, citation-shaped sentences pulled FROM the
  body (paraphrase allowed) capturing the document's main claims or findings.
  Each sentence < 200 chars. These feed the verification layer's
  cross-source consensus task.

Return ONE JSON object exactly in this shape — no surrounding prose, no
markdown fences:

{
  "documents": [
    {
      "doc_id": "<id exactly as it appears in the === doc_<id> === header>",
      "summary": "...",
      "keywords": ["...", "..."],
      "key_points": ["...", "..."]
    }
  ]
}

Rules:
- Include every document from the input exactly once, keyed by its doc_id.
- A document that is mostly empty or mostly chrome gets an empty summary,
  empty keywords, and empty key_points (but still appears in the output).
- Language policy: write summary / keywords / key_points in each body's
  dominant language (Korean if the body is Korean, otherwise English).
  Preserve URLs, code identifiers, model names, and citations as-is."""


__all__ = ["BATCH_DOC_METADATA_PROMPT", "DOCUMENT_CLEANUP_PROMPT"]
