from dataclasses import asdict

from core.models import IndexedDocRecord


class RecordSerializer:
    def serialize_records(self, records: list[IndexedDocRecord]) -> list[dict]:
        return [asdict(r) for r in records]

    def deserialize_records(self, payload: list[dict]) -> list[IndexedDocRecord]:
        return [IndexedDocRecord(**item) for item in payload]
