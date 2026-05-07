from dataclasses import asdict

from core.models import DocRecord


class RecordSerializer:
    def serialize_records(self, records: list[DocRecord]) -> list[dict]:
        return [asdict(r) for r in records]

    def deserialize_records(self, payload: list[dict]) -> list[DocRecord]:
        return [DocRecord(**item) for item in payload]