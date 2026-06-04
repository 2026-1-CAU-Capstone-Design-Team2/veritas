from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.knowledge_models import PrivacyLabel, SourceKind, SourceScope
from core.models import ParsedDocRecord
from core.verification_crosscheck_models import CrossCheckArtifact
from services.knowledge import KnowledgePackBuilder, RetrievalService
from services.local_corpus import LocalCorpusService, ManifestRepository
from services.verification.crosscheck import run_crosscheck_pipeline


class FakeLLM:
    def embed(self, _text):
        return [0.0, 1.0]

    def embed_batch(self, texts):
        return [[0.0, float(index + 1)] for index, _ in enumerate(texts)]


class FakeVectorStore:
    def __init__(self):
        self.upserts: list[dict] = []
        self.deleted_where: list[dict] = []
        self.deleted_ids: list[str] = []
        self.query_results: list[dict] = []

    def add_documents(self, doc_ids, contents, embeddings=None, metadatas=None):
        self.upserts.append(
            {
                "doc_ids": list(doc_ids),
                "contents": list(contents),
                "embeddings": embeddings,
                "metadatas": list(metadatas or []),
            }
        )

    def delete_where(self, where):
        self.deleted_where.append(dict(where))

    def delete_documents(self, doc_ids):
        self.deleted_ids.extend(doc_ids)

    def query(self, **_kwargs):
        return list(self.query_results)

    def get_all(self, where=None):
        return []


class LocalCorpusMvpTests(unittest.TestCase):
    def test_local_corpus_indexes_private_sources_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            (input_dir / "policy.md").write_text(
                "# Internal Policy\n\nRevenue target is 42 units.",
                encoding="utf-8",
            )
            (input_dir / "metrics.csv").write_text(
                "name,value\nrevenue,42\ncost,8\n",
                encoding="utf-8",
            )
            vector = FakeVectorStore()
            service = LocalCorpusService(
                output_root=root / "runs",
                llm=FakeLLM(),
                manifest_repository=ManifestRepository(root / "runs"),
                vector_store=vector,
            )

            result = service.index_workspace_sources("ws1", [str(input_dir)])

            self.assertEqual(result.failed_count, 0)
            self.assertEqual(result.indexed_count, 2)
            self.assertGreater(result.vector_count, 0)
            self.assertEqual(len(vector.upserts), 1)
            metadatas = vector.upserts[0]["metadatas"]
            self.assertTrue(metadatas)
            self.assertTrue(all(m["source_scope"] == "local" for m in metadatas))
            self.assertTrue(all(m["privacy_label"] == "local_private" for m in metadatas))

            sources = service.list_sources("ws1")
            self.assertEqual({s.source_scope for s in sources}, {SourceScope.LOCAL})
            self.assertEqual({s.privacy_label for s in sources}, {PrivacyLabel.LOCAL_PRIVATE})

    def test_reindex_modified_local_file_replaces_same_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            local_file = input_dir / "policy.md"
            local_file.write_text("# Internal Policy\n\nTarget is 42.", encoding="utf-8")
            vector = FakeVectorStore()
            service = LocalCorpusService(
                output_root=root / "runs",
                llm=FakeLLM(),
                manifest_repository=ManifestRepository(root / "runs"),
                vector_store=vector,
            )

            first = service.index_workspace_sources("ws1", [str(input_dir)])
            first_source_id = first.sources[0]["sourceId"]
            local_file.write_text("# Internal Policy\n\nTarget is 43.", encoding="utf-8")
            second = service.index_workspace_sources("ws1", [str(input_dir)])

            self.assertEqual(second.indexed_count, 1)
            self.assertEqual(len(second.sources), 1)
            self.assertEqual(second.sources[0]["sourceId"], first_source_id)
            self.assertIn({"source_id": first_source_id}, vector.deleted_where)

    def test_clear_local_first_with_no_roots_clears_private_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            (input_dir / "policy.md").write_text("Internal target is 42.", encoding="utf-8")
            vector = FakeVectorStore()
            service = LocalCorpusService(
                output_root=root / "runs",
                llm=FakeLLM(),
                manifest_repository=ManifestRepository(root / "runs"),
                vector_store=vector,
            )
            service.index_workspace_sources("ws1", [str(input_dir)])

            result = service.index_workspace_sources(
                "ws1",
                [],
                clear_local_first=True,
            )

            self.assertEqual(result.sources, [])
            self.assertEqual(service.list_sources("ws1"), [])
            self.assertIn({"source_scope": "local"}, vector.deleted_where)

    def test_retrieval_filters_local_and_external_chunks(self) -> None:
        vector = FakeVectorStore()
        vector.query_results = [
            {
                "doc_id": "local_a:chunk_000",
                "content": "Local private metric says 42 units.",
                "metadata": {
                    "workspace_id": "ws1",
                    "source_id": "local_a",
                    "source_scope": "local",
                    "source_kind": "markdown",
                    "privacy_label": "local_private",
                    "title": "internal.md",
                },
                "distance": 0.1,
            },
            {
                "doc_id": "doc_1:chunk_000",
                "content": "External web page says 39 units.",
                "metadata": {
                    "workspace_id": "ws1",
                    "source_id": "doc_1",
                    "source_scope": "external",
                    "source_kind": "web_page",
                    "privacy_label": "public_web",
                    "title": "external",
                },
                "distance": 0.2,
            },
        ]
        retrieval = RetrievalService(llm=FakeLLM(), vector_store=vector)

        local = retrieval.retrieve(
            "ws1",
            "metric",
            source_scopes={SourceScope.LOCAL},
        )

        self.assertEqual(len(local), 1)
        self.assertEqual(local[0].source_scope, SourceScope.LOCAL)
        self.assertEqual(local[0].privacy_label, PrivacyLabel.LOCAL_PRIVATE)

    def test_crosscheck_detects_numeric_mismatch_between_external_and_local(self) -> None:
        external = ParsedDocRecord(
            doc_id="1",
            title="External",
            key_points=["Revenue target for Project Atlas is 39 units in 2026."],
        )
        from core.knowledge_models import KnowledgeSourceRecord

        local_source = KnowledgeSourceRecord(
            source_id="local_a",
            workspace_id="ws1",
            source_scope=SourceScope.LOCAL,
            source_kind=SourceKind.MARKDOWN,
            title="internal.md",
            canonical_uri="internal.md",
            display_path="internal.md",
            privacy_label=PrivacyLabel.LOCAL_PRIVATE,
            content_hash="hash",
        )

        artifact = run_crosscheck_pipeline(
            external_docs=[external],
            local_sources=[local_source],
            local_documents={
                "local_a": "Internal plan: Revenue target for Project Atlas is 42 units in 2026."
            },
        )

        self.assertIsInstance(artifact, CrossCheckArtifact)
        self.assertTrue(any(r.relation == "numeric_mismatch" for r in artifact.relations))
        self.assertTrue(artifact.flags)

    def test_knowledge_pack_builder_separates_local_evidence_and_source_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vector = FakeVectorStore()
            vector.query_results = [
                {
                    "doc_id": "local_a:chunk_000",
                    "content": "Internal launch plan requires review by Legal.",
                    "metadata": {
                        "workspace_id": "ws1",
                        "source_id": "local_a",
                        "source_scope": "local",
                        "source_kind": "markdown",
                        "privacy_label": "local_private",
                        "title": "launch.md",
                        "display_path": "policies/launch.md",
                    },
                    "distance": 0.05,
                }
            ]
            retrieval = RetrievalService(llm=FakeLLM(), vector_store=vector)
            builder = KnowledgePackBuilder(
                retrieval_service=retrieval,
                workspace_root=Path(tmp),
            )

            pack = builder.build_for_outline("ws1", ["Launch Governance"])

            self.assertIn("Local Private Evidence", pack.global_context)
            self.assertIn("local_a", pack.source_map["sources"])
            self.assertEqual(
                pack.section_packs[0].local_evidence[0].privacy_label,
                PrivacyLabel.LOCAL_PRIVATE,
            )


if __name__ == "__main__":
    unittest.main()
