"""Embedding-based working-fact extraction: contrast + regex fallback."""

from __future__ import annotations

import math
import unittest

from services.memory_tools_funcs.main_context.embedding_fact_extractor import (
    EmbeddingFactExtractor,
)


class _NoEmbedLLM:
    """No embed method → extractor must fall back to regex."""


class _WordEmbedLLM:
    """Deterministic word-bucket embedding for structural tests."""

    def embed(self, text, dim: int = 96):
        vec = [0.0] * dim
        for w in str(text).split():
            vec[hash(w) % dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class FallbackTests(unittest.TestCase):
    def test_regex_fallback_when_no_embed(self) -> None:
        ex = EmbeddingFactExtractor(_NoEmbedLLM())
        self.assertEqual(ex.extract("내 이름은 박서원"), [("name", "박서원")])
        self.assertEqual(ex.extract("오늘 날씨 어때?"), [])

    def test_regex_fallback_on_embed_error(self) -> None:
        class _BrokenEmbed:
            def embed(self, text):
                raise RuntimeError("endpoint down")

        ex = EmbeddingFactExtractor(_BrokenEmbed())
        # anchor embedding fails → falls back to regex for every call
        self.assertEqual(ex.extract("제 이름은 Dana"), [("name", "Dana")])


class ContrastTests(unittest.TestCase):
    def test_attribute_declaration_is_captured(self) -> None:
        ex = EmbeddingFactExtractor(_WordEmbedLLM())
        out = ex.extract("저는 OOO를 선호해요")
        self.assertTrue(out)
        self.assertEqual(out[0][0], "preference")

    def test_chatter_is_rejected(self) -> None:
        ex = EmbeddingFactExtractor(_WordEmbedLLM())
        self.assertEqual(ex.extract("그거 좀 해줄래?"), [])

    def test_empty_text(self) -> None:
        ex = EmbeddingFactExtractor(_WordEmbedLLM())
        self.assertEqual(ex.extract("   "), [])


if __name__ == "__main__":
    unittest.main()
