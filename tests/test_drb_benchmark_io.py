from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.drb import drb_io


class IterJsonObjectsTests(unittest.TestCase):
    def test_normal_jsonl(self) -> None:
        text = '{"id": 1, "prompt": "a"}\n{"id": 2, "prompt": "b"}\n'
        objs = list(drb_io.iter_json_objects(text))
        self.assertEqual([o["id"] for o in objs], [1, 2])

    def test_concatenated_object_stream(self) -> None:
        # No newline separators between objects.
        text = '{"id": 1, "prompt": "a"}{"id": 2, "prompt": "b"}'
        objs = list(drb_io.iter_json_objects(text))
        self.assertEqual([o["id"] for o in objs], [1, 2])

    def test_embedded_newlines_in_values(self) -> None:
        text = '{"id": 1, "prompt": "line1\\nline2", "article": "x\\ny"}\n'
        objs = list(drb_io.iter_json_objects(text))
        self.assertEqual(len(objs), 1)
        self.assertIn("\n", objs[0]["prompt"])

    def test_skips_unparseable_line_and_continues(self) -> None:
        text = 'garbage not json\n{"id": 2, "prompt": "b"}\n'
        objs = list(drb_io.iter_json_objects(text))
        self.assertEqual([o["id"] for o in objs], [2])

    def test_leading_bom_does_not_drop_first_record(self) -> None:
        # A UTF-8 BOM (e.g. from PowerShell Set-Content -Encoding utf8) must not
        # eat the first object.
        text = chr(0xFEFF) + '{"id": 1, "prompt": "a"}\n{"id": 2, "prompt": "b"}\n'
        objs = list(drb_io.iter_json_objects(text))
        self.assertEqual([o["id"] for o in objs], [1, 2])


class LoadTasksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "query.jsonl"
        rows = [
            {"id": 1, "topic": "Finance & Business", "language": "zh", "prompt": "a"},
            {"id": 2, "topic": "Science", "language": "en", "prompt": "b"},
            {"id": 3, "topic": "Finance & Business", "language": "en", "prompt": "c"},
            {"id": 4, "language": "en"},  # missing prompt → skipped
        ]
        self.path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_loads_and_preserves_topic_language(self) -> None:
        tasks = drb_io.load_tasks(self.path)
        self.assertEqual([t.id for t in tasks], [1, 2, 3])  # id 4 skipped (no prompt)
        self.assertEqual(tasks[0].language, "zh")
        self.assertEqual(tasks[0].topic, "Finance & Business")

    def test_limit_applies_after_filters(self) -> None:
        tasks = drb_io.load_tasks(self.path, limit=2)
        self.assertEqual([t.id for t in tasks], [1, 2])

    def test_task_id_filter(self) -> None:
        tasks = drb_io.load_tasks(self.path, task_ids=[2])
        self.assertEqual([t.id for t in tasks], [2])

    def test_language_filter(self) -> None:
        tasks = drb_io.load_tasks(self.path, languages="en")
        self.assertEqual([t.id for t in tasks], [2, 3])

    def test_topic_filter_case_insensitive(self) -> None:
        tasks = drb_io.load_tasks(self.path, topics=["finance & business"])
        self.assertEqual([t.id for t in tasks], [1, 3])


class RawWriterAndResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self._tmp.name) / "sub" / "model.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_official_writer_emits_only_official_keys(self) -> None:
        drb_io.write_raw_jsonl(
            self.raw,
            [{"id": 1, "prompt": "p", "article": "a", "leak": "SECRET", "elapsed": 9}],
        )
        rows = list(drb_io.iter_json_objects(self.raw.read_text(encoding="utf-8")))
        self.assertEqual(set(rows[0].keys()), {"id", "prompt", "article"})
        self.assertNotIn("leak", rows[0])

    def test_append_official_row_accumulates(self) -> None:
        drb_io.append_official_row(self.raw, 1, "p1", "a1")
        drb_io.append_official_row(self.raw, 2, "p2", "a2")
        rows = list(drb_io.iter_json_objects(self.raw.read_text(encoding="utf-8")))
        self.assertEqual([r["id"] for r in rows], [1, 2])

    def test_meta_sidecar_path_and_write(self) -> None:
        drb_io.append_meta_row(self.raw, {"task_id": 1, "success": True})
        meta_path = drb_io.meta_path_for(self.raw)
        self.assertTrue(meta_path.name.endswith(".jsonl.meta.jsonl"))
        metas = list(drb_io.iter_json_objects(meta_path.read_text(encoding="utf-8")))
        self.assertEqual(metas[0]["task_id"], 1)

    def test_completed_ids_for_resume(self) -> None:
        drb_io.append_official_row(self.raw, 1, "p1", "a1")
        drb_io.append_official_row(self.raw, 2, "p2", "")  # empty article → not done
        done = drb_io.completed_task_ids(self.raw)
        self.assertEqual(done, {"1"})

    def test_completed_ids_missing_file(self) -> None:
        self.assertEqual(drb_io.completed_task_ids(self.raw), set())


if __name__ == "__main__":
    unittest.main()
