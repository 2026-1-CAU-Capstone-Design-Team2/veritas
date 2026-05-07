from bs4.element import Tag
import re

from .hints import BOILERPLATE_HINT, MAIN_CONTENT_HINT

def _strip_noise_tags(root: Tag) -> None:
    for tag in root(["script", "style", "noscript", "iframe", "svg", "canvas", "form", "button"]):
        tag.decompose()
    for tag in root(["nav", "header", "footer", "aside"]):
        tag.decompose()

def _candidate_nodes(root: Tag) -> list[Tag]:
    candidates: list[Tag] = []
    for selector in (
        "article",
        "main",
        "[role='main']",
        "section",
        "div",
    ):
        candidates.extend(root.select(selector))
    if not candidates:
        return [root]
    return candidates

def _node_hint_text(node: Tag) -> str:
    attrs = [
        node.get("id", ""),
        " ".join(node.get("class", [])) if node.get("class") else "",
        node.name or "",
    ]
    return " ".join(attrs)

def _content_score(node: Tag) -> float:
    text = node.get_text(" ", strip=True)
    if not text:
        return -1.0

    text_len = len(text)
    if text_len < 120:
        return -1.0

    link_text_len = sum(len(a.get_text(" ", strip=True)) for a in node.find_all("a"))
    link_density = (link_text_len / text_len) if text_len else 1.0

    p_count = len(node.find_all("p"))
    h_count = len(node.find_all(["h1", "h2", "h3"]))
    hint = _node_hint_text(node)

    score = float(text_len)
    score += p_count * 80.0
    score += h_count * 25.0
    if MAIN_CONTENT_HINT.search(hint):
        score += 1500.0
    if BOILERPLATE_HINT.search(hint):
        score -= 2000.0
    score -= link_density * 2500.0
    return score

def _select_main_content_node(root: Tag) -> Tag:
    candidates = _candidate_nodes(root)
    best = root
    best_score = _content_score(root)

    for node in candidates:
        score = _content_score(node)
        if score > best_score:
            best = node
            best_score = score
    return best

def _extract_meaningful_text(node: Tag, max_chars: int) -> str:
    parts: list[str] = []
    # Prefer semantically rich blocks first.
    for el in node.find_all(["h1", "h2", "h3", "p", "li", "blockquote", "pre"]):
        segment = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if not segment:
            continue

        # Drop menu-like short labels unless they look like a meaningful sentence.
        if len(segment) < 25 and not re.search(r"[.!?]|[0-9]", segment):
            continue
        parts.append(segment)

        if sum(len(p) + 1 for p in parts) >= max_chars:
            break

    if not parts:
        fallback = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        return fallback[:max_chars]

    text = "\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:max_chars]