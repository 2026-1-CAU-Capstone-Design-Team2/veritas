"""Pre-fetch source-candidate scoring for AutoSurvey.

The collect loop used to fetch every search result in order, so off-topic hits
(an *AI video market* report inside a 대체육 survey, a *bath bomb market* report,
a domain that floods the page) consumed fetch time, cleanup LLM calls, and a
``maxDocs`` slot before anyone could tell they were irrelevant. This module
ranks candidates *before* fetch using only **structural / lexical** signal —
token overlap between a candidate's title / snippet / url and the terms already
present in the user request, the plan, and the live query — so the highest-value
levers (fewer wasted fetches, fewer downstream LLM calls) happen up front.

Hard rules (mirrors the cleanup module's generalizability rule):

* **No topic-, site-, or language-specific keyword lists.** Relevance is the
  overlap of a candidate with terms the *user/plan/query themselves* supplied —
  never a hard-coded vocabulary of "good"/"bad" words or domains.
* Tokenization is structural: Latin word runs and Korean syllable runs of
  length ≥ 2, plus standalone numbers (a shared figure is a strong signal).
* The score is a *precision* ratio — what fraction of a candidate's own tokens
  are on-topic — so an off-topic result that carries its own subject tokens
  (``ai``/``video`` or ``bath``/``bomb``) is pushed down even when it shares the
  generic market words (``market``/``시장``/``규모``) every report repeats.

Two further gates layer on top of the lexical score, both structural (not
keyword lists):

* **Entity anchor (post-fetch).** A flat token-overlap count cannot tell
  *삼성전자 실적* from *쿠팡 실적*: both share the generic financial vocabulary
  (``4분기``/``실적``/``매출``/``영업이익``) and clear the hit threshold, so a
  wrong-company document slips in. When the run carries *anchor terms* — the
  named subjects ``term_grounding`` extracted from the user's own request, e.g.
  ``삼성전자`` — a fetched body must mention at least one of them to be kept.
  Anchors are per-request and LLM-derived, never a hard-coded vocabulary; an
  empty anchor set (a purely conceptual request) disables the gate entirely, so
  conceptual surveys behave exactly as before.
* **Homepage root (pre-fetch).** A bare site root (``https://site.com/`` — empty
  or ``/`` path, no query) is navigation chrome, not an evidence document, yet it
  often echoes the topic in its headlines and would pass every lexical check.
  Such roots are dropped before fetch on URL *shape* alone. User-pinned
  reference sites are exempt (the user deliberately pinned that root).

The filter never starves collection: when the topic signal is too thin to judge
(a degenerate/empty plan) or every candidate scores below threshold, the ranking
is still applied but nothing is dropped — ``maxDocs`` and the downstream cleanup
gate remain the real backstops.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlparse


_LATIN_RE = re.compile(r"[A-Za-z]{2,}")
_HANGUL_RE = re.compile(r"[가-힣]{2,}")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
# Hyphen/underscore/slash/apostrophe are word separators, not part of a token,
# so "plant-based" and "plant based" tokenize identically (plant, based).
_SEP_RE = re.compile(r"[-_'/]+")

# A candidate is dropped only when it is clearly off-topic; the default is
# deliberately permissive (the cleanup gate and maxDocs are the real backstops).
_DEFAULT_MIN_SCORE = 0.18
# Per-domain cap so one site can't flood a cycle. Reference sites are exempt.
_DEFAULT_DOMAIN_CAP = 3
# Below this many topic content tokens the signal is too thin to filter on —
# only reorder, never drop.
_MIN_TOPIC_TOKENS = 3
# A fetched body must mention at least this many distinct topic terms to be
# kept; below it the page is off-topic/empty (anti-bot, redirect, wrong
# subject) and is rejected without consuming a maxDocs slot. Conservative on
# purpose — the pre-fetch gate already drops the obvious off-topic snippets, so
# this only catches bodies whose overlap with the topic is near zero.
_MIN_BODY_TOPIC_HITS = 2

# Snippet text can live under any of these keys depending on the search backend.
_SNIPPET_KEYS = ("snippet", "body", "summary", "description", "content")


@dataclass(frozen=True)
class TopicTerms:
    """The on-topic vocabulary, derived from request + plan + query.

    ``anchors`` is a separate, *required-presence* set: the named subjects the
    research must stay about (``term_grounding``'s ``candidate_entities``). Unlike
    ``content``/``numbers`` — which are counted for an overlap *score* — an anchor
    is a hard precondition for keeping a fetched body when the set is non-empty.
    """

    content: frozenset[str]
    numbers: frozenset[str]
    anchors: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_thin(self) -> bool:
        return len(self.content) < _MIN_TOPIC_TOKENS

    @property
    def has_anchors(self) -> bool:
        return bool(self.anchors)


@dataclass(frozen=True)
class ScoredCandidate:
    """One search result with its relevance verdict (original order preserved)."""

    index: int
    url: str
    domain: str
    title: str
    score: float
    kept: bool
    reason: str  # "kept" | "reference_site" | "low_relevance" | "domain_cap"
    item: dict


def _tokenize(text: str) -> tuple[set[str], set[str]]:
    text = _SEP_RE.sub(" ", str(text or "").lower())
    content = set(_LATIN_RE.findall(text))
    content |= set(_HANGUL_RE.findall(text))
    numbers = {n.replace(",", "") for n in _NUMBER_RE.findall(text)}
    return content, numbers


def _url_text(url: str) -> str:
    """Path/host words carry topic signal (``/plant-based-meat-market``)."""
    parsed = urlparse(str(url or ""))
    return f"{parsed.netloc} {parsed.path}"


def _normalize_anchor_terms(anchor_terms: Iterable[str]) -> frozenset[str]:
    """Lower-case, de-noise the anchor phrases used for the entity gate.

    Drops empties, single characters, and bare numbers — a year such as
    ``2024`` is shared by every report and would defeat the gate. Multi-word
    phrases are kept whole; matching (see :func:`_anchor_present`) handles them.
    """
    out: set[str] = set()
    for term in anchor_terms or ():
        text = " ".join(str(term or "").lower().split())
        if len(text) < 2:
            continue
        if _NUMBER_RE.fullmatch(text):
            continue
        out.add(text)
    return frozenset(out)


def _anchor_present(text_lower: str, anchor: str) -> bool:
    """Whether *anchor* occurs in already-lower-cased *text_lower*.

    Korean and longer anchors use substring containment, which is robust to the
    agglutinative particles Korean attaches to a name (``삼성전자의``/``삼성전자가``
    all contain ``삼성전자``) — exact token equality would miss those. Short
    Latin/numeric anchors fall back to a word-boundary match so a 2-3 char token
    like ``ai`` does not spuriously hit inside ``rain``/``training``.
    """
    if not anchor:
        return False
    if _HANGUL_RE.search(anchor) or len(anchor) >= 4:
        return anchor in text_lower
    return re.search(rf"(?<![a-z0-9]){re.escape(anchor)}(?![a-z0-9])", text_lower) is not None


def candidate_domain(url: str) -> str:
    netloc = urlparse(str(url or "")).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _matches_reference(domain: str, reference_domains: frozenset[str]) -> bool:
    """True when *domain* is a pinned reference domain or a subdomain of one.

    A user-pinned ``samsung.com`` exempts ``news.samsung.com`` /
    ``www.samsung.com`` too. Structural suffix match — not a site allowlist.
    """
    return any(
        bool(ref) and (domain == ref or domain.endswith("." + ref))
        for ref in reference_domains
    )


def _candidate_snippet(item: dict) -> str:
    for key in _SNIPPET_KEYS:
        value = item.get(key)
        if value:
            return str(value)
    return ""


def build_topic_terms(
    *,
    user_request: str = "",
    plan: dict | None = None,
    query: str = "",
    anchor_terms: Iterable[str] = (),
) -> TopicTerms:
    """Union the on-topic vocabulary from the request, plan, and live query.

    Only the human-meaningful plan fields are used (topic / goal / must_cover /
    keywords) — never ``search_queries`` (those are tactics, not topic) and never
    a hard-coded list.

    ``anchor_terms`` are the named subjects the research must remain about (the
    grounding's ``candidate_entities``). They are carried separately as a
    *required-presence* set and never folded into the overlap score; an empty
    set leaves behaviour identical to before.
    """
    plan = plan if isinstance(plan, dict) else {}
    parts: list[str] = [str(user_request or ""), str(query or "")]
    parts.append(str(plan.get("topic") or ""))
    parts.append(str(plan.get("goal") or ""))
    for key in ("must_cover", "keywords"):
        value = plan.get(key)
        if isinstance(value, list):
            parts.extend(str(x) for x in value)
    content, numbers = _tokenize(" ".join(parts))
    return TopicTerms(
        content=frozenset(content),
        numbers=frozenset(numbers),
        anchors=_normalize_anchor_terms(anchor_terms),
    )


def score_candidate(item: dict, topic: TopicTerms) -> float:
    """Fraction of a candidate's own informative tokens that are on-topic.

    Precision, not recall: a long off-topic snippet that happens to mention
    ``market`` still scores low because most of its tokens (its real subject)
    miss the topic set. Returns 0.0 for an empty/contentless candidate.
    """
    title = str(item.get("title") or "")
    snippet = _candidate_snippet(item)
    url = str(item.get("link") or item.get("url") or "")
    content, numbers = _tokenize(f"{title} {snippet} {_url_text(url)}")
    pool = content | numbers
    if not pool:
        return 0.0
    shared = len(content & topic.content) + len(numbers & topic.numbers)
    return round(shared / len(pool), 4)


def topic_hit_count(text: str, topic: TopicTerms) -> int:
    """How many distinct topic terms (content + numbers) appear in *text*."""
    content, numbers = _tokenize(text)
    return len(content & topic.content) + len(numbers & topic.numbers)


def body_has_anchor(text: str, topic: TopicTerms) -> bool:
    """Whether *text* mentions at least one of the topic's named-subject anchors.

    Vacuously ``True`` when the topic carries no anchors (conceptual request), so
    the entity gate is opt-in and never narrows a survey that has no specific
    named subject to anchor on.
    """
    if not topic.anchors:
        return True
    text_lower = str(text or "").lower()
    return any(_anchor_present(text_lower, anchor) for anchor in topic.anchors)


def body_is_on_topic(
    text: str, topic: TopicTerms, *, min_hits: int = _MIN_BODY_TOPIC_HITS
) -> bool:
    """Whether a *fetched body* overlaps the topic enough to keep the document.

    Used post-fetch (the body, not the snippet): a page that shares almost no
    terms with the request/plan is off-topic or empty and should be rejected
    rather than consume a maxDocs slot. Returns ``True`` (never reject) when the
    topic signal is too thin to judge.

    When the topic carries named-subject anchors, the body must additionally
    mention at least one of them — this is what stops a *쿠팡 실적* page from
    being kept in a *삼성전자 실적* survey purely on shared financial vocabulary.
    """
    if topic.is_thin:
        return True
    if not body_has_anchor(text, topic):
        return False
    return topic_hit_count(text, topic) >= min_hits


def is_homepage_root(url: str) -> bool:
    """Whether *url* is a bare site root (homepage), not a specific document.

    Structural only: a netloc with an empty or ``/`` path and no query string is
    site navigation chrome (``https://news.site.com/``), never a single piece of
    evidence. A fragment is ignored (``/#section`` is still the homepage). Any
    real path segment (``/article/123``) makes it not a root.
    """
    parsed = urlparse(str(url or ""))
    if not parsed.netloc:
        return False
    if (parsed.path or "").strip() not in ("", "/"):
        return False
    return not (parsed.query or parsed.params)


def rank_candidates(
    items: list[dict],
    topic: TopicTerms,
    *,
    reference_domains: frozenset[str] = frozenset(),
    min_score: float = _DEFAULT_MIN_SCORE,
    domain_cap: int = _DEFAULT_DOMAIN_CAP,
) -> list[ScoredCandidate]:
    """Score, sort (desc, stable), and mark each candidate kept/dropped.

    * Reference-site domains are always kept (``reason="reference_site"``).
    * A bare site root / homepage is dropped on URL shape alone
      (``"homepage_root"``); reference domains are exempt.
    * A candidate below ``min_score`` is dropped (``"low_relevance"``) — unless
      the topic signal is thin or *every* candidate is below threshold, in which
      case nothing is dropped (collection is never starved).
    * After relevance, a per-domain cap drops the surplus (``"domain_cap"``);
      reference domains are exempt.
    """
    scored = [
        (
            index,
            item,
            score_candidate(item, topic),
            candidate_domain(str(item.get("link") or item.get("url") or "")),
        )
        for index, item in enumerate(items)
    ]
    # Stable sort by score desc, original order within ties.
    order = sorted(range(len(scored)), key=lambda i: (-scored[i][2], scored[i][0]))

    any_above = any(s[2] >= min_score for s in scored)
    apply_relevance_gate = any_above and not topic.is_thin

    domain_counts: dict[str, int] = {}
    results: list[ScoredCandidate] = []
    for i in order:
        index, item, score, domain = scored[i]
        url = str(item.get("link") or item.get("url") or "")
        is_reference = bool(domain) and _matches_reference(domain, reference_domains)

        if is_reference:
            kept, reason = True, "reference_site"
        elif is_homepage_root(url):
            kept, reason = False, "homepage_root"
        elif apply_relevance_gate and score < min_score:
            kept, reason = False, "low_relevance"
        else:
            kept, reason = True, "kept"

        if kept and not is_reference and domain:
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            if domain_counts[domain] > domain_cap:
                kept, reason = False, "domain_cap"

        results.append(
            ScoredCandidate(
                index=index,
                url=url,
                domain=domain,
                title=str(item.get("title") or ""),
                score=score,
                kept=kept,
                reason=reason,
                item=item,
            )
        )
    return results


__all__ = [
    "TopicTerms",
    "ScoredCandidate",
    "build_topic_terms",
    "score_candidate",
    "rank_candidates",
    "candidate_domain",
    "topic_hit_count",
    "body_has_anchor",
    "body_is_on_topic",
    "is_homepage_root",
]
