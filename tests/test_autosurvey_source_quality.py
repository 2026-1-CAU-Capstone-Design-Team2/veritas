"""Pre-fetch source-candidate scoring (services/autosurvey_source_quality.py)."""

from __future__ import annotations

import unittest

from services.autosurvey_source_quality import (
    _anchor_present,
    body_has_anchor,
    body_is_on_topic,
    build_topic_terms,
    candidate_domain,
    is_homepage_root,
    rank_candidates,
    score_candidate,
    topic_hit_count,
)


_PLAN = {
    "topic": "대체육 식물성 단백질 시장",
    "goal": "국내외 시장 규모와 성장 전망, 주요 업체 동향 조사",
    "keywords": ["plant-based meat", "대체육", "식물성 단백질"],
    "must_cover": ["국내 시장 규모", "글로벌 시장 전망"],
    "search_queries": ["SECRETQUERYTOKEN"],
}


def _topic(query: str = "국내외 대체육 시장 규모 전망"):
    return build_topic_terms(plan=_PLAN, query=query)


def _item(title: str, link: str, snippet: str = "") -> dict:
    return {"title": title, "link": link, "snippet": snippet}


_RELEVANT = _item(
    "Plant-based meat market size and growth forecast",
    "https://reporthub.com/plant-based-meat-market",
    "The global plant-based meat 대체육 시장 규모 and 식물성 단백질 growth outlook.",
)
# Shares only the generic market words (시장/규모) — its real subject misses.
_OFFTOPIC_GENERIC = _item(
    "AI video generation market size",
    "https://techwire.com/ai-video-market",
    "The AI 영상 생성 video market 시장 규모 outlook and vendor landscape.",
)
# No topic overlap at all.
_OFFTOPIC_CLEAN = _item(
    "Bath bomb industry trends and retail report",
    "https://lifestyle.com/bath-bomb-trends",
    "Latest bath bomb consumer analysis and retail channel breakdown.",
)


class BuildTopicTermsTests(unittest.TestCase):
    def test_search_queries_are_not_topic_signal(self) -> None:
        topic = _topic()
        self.assertNotIn("secretquerytoken", topic.content)
        self.assertIn("대체육", topic.content)
        self.assertIn("plant", topic.content)

    def test_empty_plan_is_thin(self) -> None:
        self.assertTrue(build_topic_terms(plan={}, query="").is_thin)

    def test_user_request_terms_enter_topic_and_lift_ranking(self) -> None:
        # A constraint living ONLY in the request (not plan/query) must still
        # influence the topic vocabulary and a candidate's score.
        plan = {"topic": "시장 조사", "search_queries": ["SECRETQUERYTOKEN"]}
        without = build_topic_terms(plan=plan, query="시장")
        with_req = build_topic_terms(
            user_request="대체육 식물성 단백질 동향", plan=plan, query="시장"
        )
        self.assertIn("대체육", with_req.content)
        self.assertNotIn("대체육", without.content)
        self.assertNotIn("secretquerytoken", with_req.content)  # queries excluded
        item = _item("대체육 식물성 단백질 리포트", "https://e.com/x", "대체육 동향 분석")
        self.assertGreater(score_candidate(item, with_req), score_candidate(item, without))


class ScoreCandidateTests(unittest.TestCase):
    def test_relevant_outranks_offtopic(self) -> None:
        topic = _topic()
        relevant = score_candidate(_RELEVANT, topic)
        generic = score_candidate(_OFFTOPIC_GENERIC, topic)
        clean = score_candidate(_OFFTOPIC_CLEAN, topic)
        self.assertGreater(relevant, generic)
        self.assertGreater(relevant, clean)
        self.assertEqual(clean, 0.0)  # nothing on-topic

    def test_contentless_candidate_scores_zero(self) -> None:
        self.assertEqual(score_candidate({"title": "", "link": ""}, _topic()), 0.0)


class BodyOnTopicTests(unittest.TestCase):
    def test_relevant_body_is_kept(self) -> None:
        body = (
            "본 보고서는 글로벌 plant-based meat(대체육) 시장 규모와 식물성 단백질 "
            "성장 전망, 주요 업체 동향을 다룬다. 2025년 시장은..."
        )
        self.assertGreaterEqual(topic_hit_count(body, _topic()), 2)
        self.assertTrue(body_is_on_topic(body, _topic()))

    def test_offtopic_body_is_rejected(self) -> None:
        body = (
            "This guide reviews the best bath bomb recipes, essential oils, and "
            "fragrance blends for a relaxing home spa experience."
        )
        self.assertLess(topic_hit_count(body, _topic()), 2)
        self.assertFalse(body_is_on_topic(body, _topic()))

    def test_thin_topic_never_rejects(self) -> None:
        thin = build_topic_terms(plan={}, query="")
        self.assertTrue(body_is_on_topic("anything at all", thin))

    def test_query_drift_does_not_pass_core_acceptance_gate(self) -> None:
        # A replan query drifts toward generic video-diffusion terms. The full
        # topic (incl. query) would accept an off-topic body, but the core topic
        # (request + plan only) — what the post-fetch gate must use — rejects it.
        plan = {"topic": "텍스트 확산 언어모델", "keywords": ["text diffusion LM", "DLM"]}
        request = "텍스트 기반 diffusion language model 비교 분석"
        drift_query = "DiT video diffusion latency optimization GPU"
        core = build_topic_terms(user_request=request, plan=plan, query="")
        full = build_topic_terms(user_request=request, plan=plan, query=drift_query)
        body = "This paper studies DiT video diffusion latency optimization on GPUs and TPUs."
        self.assertTrue(body_is_on_topic(body, full))   # query-only match (the bug)
        self.assertFalse(body_is_on_topic(body, core))  # core gate rejects (the fix)


# Mirrors the real bug: a 삼성전자 실적 survey collecting 쿠팡/에이블리 earnings
# pages, which share the generic financial vocabulary but not the entity.
_SAMSUNG_PLAN = {
    "topic": "삼성전자 2024년 4분기 잠정 실적",
    "goal": "삼성전자 분기 매출과 영업이익 확인",
    "keywords": ["삼성전자", "4분기", "실적", "매출", "영업이익"],
}


def _samsung_topic(anchors=("삼성전자",)):
    return build_topic_terms(
        user_request="삼성전자 작년 4분기 잠정 실적 알려줘",
        plan=_SAMSUNG_PLAN,
        query="삼성전자 4분기 실적",
        anchor_terms=anchors,
    )


_SAMSUNG_BODY = (
    "삼성전자는 2024년 4분기 잠정 실적을 발표했다. 매출 70조 4,600억원, "
    "영업이익 4조 3,100억원을 기록했다."
)
# Shares 4분기/2024/실적/매출/영업이익 with the topic but never names 삼성전자.
_COUPANG_BODY = (
    "쿠팡은 2024년 4분기 실적을 공개했다. 매출 88억 달러, 영업이익은 "
    "전년 동기 대비 크게 줄며 적자로 전환했다."
)


class EntityAnchorTests(unittest.TestCase):
    def test_wrong_entity_body_clears_old_count_gate(self) -> None:
        # Without the anchor gate the 쿠팡 body would be kept: it shares enough
        # generic financial terms to pass the >=2-hit count gate. This is the bug.
        topic = _samsung_topic()
        self.assertGreaterEqual(topic_hit_count(_COUPANG_BODY, topic), 2)

    def test_wrong_entity_body_is_rejected_by_anchor_gate(self) -> None:
        topic = _samsung_topic()
        self.assertFalse(body_has_anchor(_COUPANG_BODY, topic))
        self.assertFalse(body_is_on_topic(_COUPANG_BODY, topic))

    def test_right_entity_body_is_kept(self) -> None:
        topic = _samsung_topic()
        self.assertTrue(body_has_anchor(_SAMSUNG_BODY, topic))
        self.assertTrue(body_is_on_topic(_SAMSUNG_BODY, topic))

    def test_anchor_matches_through_korean_particles(self) -> None:
        # 삼성전자의 / 삼성전자가 are single Hangul runs that exact-token matching
        # would miss; substring containment keeps the anchor robust.
        body = "삼성전자의 2024년 4분기 매출과 영업이익이 공개되었다."
        self.assertTrue(body_has_anchor(body, _samsung_topic()))

    def test_no_anchors_disables_gate(self) -> None:
        # A conceptual request (no candidate_entities) must behave exactly as
        # before: the count gate alone decides.
        topic = _samsung_topic(anchors=())
        self.assertFalse(topic.has_anchors)
        self.assertTrue(body_has_anchor(_COUPANG_BODY, topic))
        self.assertTrue(body_is_on_topic(_COUPANG_BODY, topic))

    def test_bare_year_is_not_a_usable_anchor(self) -> None:
        # A standalone number must never become an anchor — it would match every
        # report and defeat the gate.
        topic = _samsung_topic(anchors=("2024",))
        self.assertFalse(topic.has_anchors)

    def test_short_latin_anchor_uses_word_boundary(self) -> None:
        self.assertTrue(_anchor_present("the new ai model", "ai"))
        self.assertFalse(_anchor_present("heavy rain is training", "ai"))


class HomepageRootTests(unittest.TestCase):
    def test_bare_roots_are_homepages(self) -> None:
        self.assertTrue(is_homepage_root("https://news.site.com/"))
        self.assertTrue(is_homepage_root("https://news.site.com"))
        self.assertTrue(is_homepage_root("https://site.com/#latest"))

    def test_real_paths_and_search_pages_are_not_homepages(self) -> None:
        self.assertFalse(is_homepage_root("https://site.com/article/123"))
        self.assertFalse(is_homepage_root("https://site.com/?q=samsung"))
        self.assertFalse(is_homepage_root("not a url"))

    def test_ontopic_homepage_is_dropped_before_fetch(self) -> None:
        homepage = _item(
            "삼성전자 뉴스룸",
            "https://example-news.com/",
            "삼성전자 4분기 실적 매출 영업이익 속보",
        )
        article = _item(
            "삼성전자 4분기 실적 상세",
            "https://example-news.com/articles/samsung-q4",
            "삼성전자 4분기 매출 영업이익",
        )
        ranked = rank_candidates([homepage, article], _samsung_topic())
        home = next(c for c in ranked if c.item is homepage)
        self.assertFalse(home.kept)
        self.assertEqual(home.reason, "homepage_root")
        art = next(c for c in ranked if c.item is article)
        self.assertTrue(art.kept)

    def test_reference_root_is_exempt_from_homepage_drop(self) -> None:
        root = _item("Samsung", "https://samsung.com/", "company site")
        ranked = rank_candidates(
            [root], _samsung_topic(), reference_domains=frozenset({"samsung.com"})
        )
        self.assertTrue(ranked[0].kept)
        self.assertEqual(ranked[0].reason, "reference_site")


class RankCandidatesTests(unittest.TestCase):
    def test_offtopic_ranked_below_and_clean_dropped(self) -> None:
        ranked = rank_candidates(
            [_OFFTOPIC_CLEAN, _OFFTOPIC_GENERIC, _RELEVANT], _topic()
        )
        # Relevant first (sorted by score desc).
        self.assertEqual(ranked[0].item, _RELEVANT)
        self.assertTrue(ranked[0].kept)
        # The clearly off-topic one is dropped as low relevance.
        clean = next(c for c in ranked if c.item is _OFFTOPIC_CLEAN)
        self.assertFalse(clean.kept)
        self.assertEqual(clean.reason, "low_relevance")

    def test_domain_cap_limits_one_site(self) -> None:
        items = [
            _item(
                f"대체육 식물성 단백질 시장 규모 리포트 {i}",
                f"https://news.example.com/article-{i}",
                "plant-based meat 시장 규모 전망",
            )
            for i in range(5)
        ]
        ranked = rank_candidates(items, _topic(), domain_cap=3)
        kept = [c for c in ranked if c.kept]
        capped = [c for c in ranked if c.reason == "domain_cap"]
        self.assertEqual(len(kept), 3)
        self.assertEqual(len(capped), 2)

    def test_reference_site_is_exempt_from_gate_and_cap(self) -> None:
        ref = "news.samsung.com"
        items = [
            # off-topic text but on a pinned reference domain
            _item("unrelated press release", f"https://{ref}/p{i}", "company news")
            for i in range(5)
        ]
        ranked = rank_candidates(
            items, _topic(), reference_domains=frozenset({ref}), domain_cap=3
        )
        self.assertTrue(all(c.kept for c in ranked))
        self.assertTrue(all(c.reason == "reference_site" for c in ranked))

    def test_parent_reference_domain_exempts_subdomains_from_gate_and_cap(self) -> None:
        # A pinned parent domain (samsung.com) exempts its subdomains, even past
        # the per-domain cap and even when off-topic.
        items = [
            _item("unrelated", f"https://news.samsung.com/p{i}", "company news")
            for i in range(5)
        ]
        ranked = rank_candidates(
            items, _topic(), reference_domains=frozenset({"samsung.com"}), domain_cap=3
        )
        self.assertTrue(all(c.kept for c in ranked))
        self.assertTrue(all(c.reason == "reference_site" for c in ranked))


class MatchesReferenceTests(unittest.TestCase):
    def test_parent_and_subdomain_match_but_lookalikes_do_not(self) -> None:
        from services.autosurvey_source_quality import _matches_reference as matches

        refs = frozenset({"samsung.com"})
        self.assertTrue(matches("samsung.com", refs))
        self.assertTrue(matches("news.samsung.com", refs))
        self.assertFalse(matches("notsamsung.com", refs))  # not a subdomain
        self.assertFalse(matches("samsung.com.evil.com", refs))  # suffix spoof

    def test_thin_topic_drops_nothing(self) -> None:
        # Degenerate plan → too little signal to judge → reorder only, never drop.
        thin = build_topic_terms(plan={}, query="")
        ranked = rank_candidates([_OFFTOPIC_CLEAN, _OFFTOPIC_GENERIC], thin)
        self.assertTrue(all(c.kept for c in ranked))

    def test_empty_results(self) -> None:
        self.assertEqual(rank_candidates([], _topic()), [])

    def test_candidate_domain_strips_www(self) -> None:
        self.assertEqual(candidate_domain("https://www.example.com/x"), "example.com")


if __name__ == "__main__":
    unittest.main()
