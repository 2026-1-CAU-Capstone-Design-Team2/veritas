__all__ = [
    "BOILERPLATE_HINT",
    "MAIN_CONTENT_HINT",
    "_strip_noise_tags",
    "_candidate_nodes",
    "_node_hint_text",
    "_content_score",
    "_select_main_content_node",
    "_extract_meaningful_text",
    "RunStoreService",
    "RunPathManager",
    "RecordSerializer",
]


def __getattr__(name: str):
    if name in {"BOILERPLATE_HINT", "MAIN_CONTENT_HINT"}:
        from .hints import BOILERPLATE_HINT, MAIN_CONTENT_HINT

        return {
            "BOILERPLATE_HINT": BOILERPLATE_HINT,
            "MAIN_CONTENT_HINT": MAIN_CONTENT_HINT,
        }[name]

    if name in {
        "_strip_noise_tags",
        "_candidate_nodes",
        "_node_hint_text",
        "_content_score",
        "_select_main_content_node",
        "_extract_meaningful_text",
    }:
        from .fetch_webpage_tool_funcs import (
            _candidate_nodes,
            _content_score,
            _extract_meaningful_text,
            _node_hint_text,
            _select_main_content_node,
            _strip_noise_tags,
        )

        return {
            "_strip_noise_tags": _strip_noise_tags,
            "_candidate_nodes": _candidate_nodes,
            "_node_hint_text": _node_hint_text,
            "_content_score": _content_score,
            "_select_main_content_node": _select_main_content_node,
            "_extract_meaningful_text": _extract_meaningful_text,
        }[name]

    if name in {"RunStoreService", "RunPathManager", "RecordSerializer"}:
        from .run_store_tool_funcs import RunPathManager, RecordSerializer, RunStoreService

        return {
            "RunStoreService": RunStoreService,
            "RunPathManager": RunPathManager,
            "RecordSerializer": RecordSerializer,
        }[name]

    raise AttributeError(f"module 'services' has no attribute {name!r}")
