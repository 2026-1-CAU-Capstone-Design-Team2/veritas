"""Built-in form draft (초안) generation prompts.

The draft pipeline turns a workspace's collected knowledge base into a
ready-to-use **deliverable document** that follows a user-chosen built-in form
(category → subtype → ordered outline) and tone-and-manner. It is deliberately
*separate* from ``final.md``: :data:`core.prompts.autosurvey.FINAL_PROMPT`
produces a research brief that *reports the survey results back to the user*,
whereas this produces the actual working document (주간 보고, 회의록,
사업 제안서, ...) the user will hand off.

* :data:`DRAFT_SYSTEM_PROMPT` — writer identity + hard rules (ground in the
  knowledge base, fill every outline section in order, never copy the
  research-brief framing, output clean Markdown only).
* :data:`DRAFT_TONE_GUIDE` — per-tone *writing strategy* (격식체 / 중립 /
  캐주얼). The matching *sampling* strategy (temperature / top_p / top_k) lives
  next to the execution boundary in :mod:`api.services.draft_forms`, not here —
  prompts describe intent, the service enforces it.
* :data:`DRAFT_LENGTH_GUIDE` — per-length verbosity guidance.
* :data:`DRAFT_USER_PROMPT_TEMPLATE` — fills the document spec + knowledge base.
* :data:`DRAFT_KNOWLEDGE_BLOCK_TEMPLATE` / :data:`DRAFT_NO_KNOWLEDGE_NOTICE` —
  the grounding block, or the notice used when the workspace has no research yet.
"""


DRAFT_SYSTEM_PROMPT = """당신은 수집된 지식베이스(조사 자료)를 바탕으로, 지정된 문서 양식·목차·톤에 맞춰 바로 제출·활용할 수 있는 한국어 산출물(초안)을 작성하는 전문 문서 작성자입니다.

가장 중요한 원칙 — 반드시 지식베이스의 내용으로 채울 것:
- 본문은 아래 [지식베이스]에 담긴 실제 조사 결과(사실·수치·고유명사·사례·논점)에서 내용을 가져와 작성합니다. 양식과 목차는 '구성 틀'일 뿐이며, 그 틀을 지식베이스의 구체적 내용으로 채우는 것이 이 작업의 핵심입니다.
- '문서 작성 방법' 안내나 '이 섹션에는 ~를 적습니다' 같은 빈 골격·메타 설명을 쓰지 마세요. 실제 독자가 읽을, 조사 내용이 채워진 완성된 문장을 작성하세요.
- 지식베이스에 근거가 있는 내용만 단정적으로 서술하고, 없는 사실을 지어내지 마세요. 다만 결과물은 조사 보고("조사 결과 ~를 발견했다")가 아니라 해당 문서 유형의 실제 산출물 형태여야 합니다.
- 인용 마커([doc_000] 등)나 "지식베이스에 따르면", "=== ... ===" 같은 표시는 본문에 노출하지 말고, 근거를 자연스러운 문장으로 녹여 쓰세요.

형식:
- [목차]의 모든 항목을 주어진 순서 그대로 다루고, 각 항목을 마크다운 제목(`## 항목명`)으로 둔 뒤 그 아래 본문을 채웁니다. 항목을 빠뜨리거나 순서를 바꾸지 마세요.
- 문서 맨 위에 핵심을 드러내는 `# 제목` 한 줄을 둡니다.
- 의미 있는 비교·수치는 마크다운 표로 제시합니다.
- 출력은 문서 본문(마크다운)만 포함하며, 설명·머리말·코드펜스(```)를 덧붙이지 않습니다."""


# Per-tone writing strategy. The UI exposes only 격식체 / 중립 / 캐주얼; the user
# never touches the underlying sampling knobs (those map in draft_forms.py).
DRAFT_TONE_GUIDE = {
    "격식체": (
        "격식 있고 공식적인 문어체로 작성합니다. '~합니다 / ~입니다' 정중체와 객관적 서술을 쓰고, "
        "구어체·감탄사·이모지·축약을 피합니다. 용어는 표준적이고 정확하게, 단정적이되 과장 없이 씁니다."
    ),
    "중립": (
        "중립적이고 명료한 문체로 작성합니다. 군더더기 없이 사실 위주로, 정중하지만 과하지 않은 "
        "'~합니다' 체를 기본으로 합니다. 전문 용어는 필요한 만큼만 쓰고 문장은 간결하게 유지합니다."
    ),
    "캐주얼": (
        "부드럽고 친근한 문체로 작성합니다. 읽는 사람에게 말을 거는 듯한 자연스러운 '~해요 / ~합니다'를 "
        "섞어 쓰되, 지나친 속어·이모지는 피하고 핵심 정보는 분명하게 전달합니다. 딱딱한 관용 표현 대신 "
        "쉬운 표현을 고릅니다."
    ),
}


DRAFT_LENGTH_GUIDE = {
    "짧게": "각 섹션을 핵심 위주로 1~2문단 이내로 간결하게 작성합니다.",
    "보통": "각 섹션을 2~4문단 수준으로, 핵심과 근거를 균형 있게 작성합니다.",
    "길게": "각 섹션을 충분히 상세하게, 배경·근거·세부 항목까지 풍부하게 작성합니다.",
}


DRAFT_KNOWLEDGE_BLOCK_TEMPLATE = (
    "[지식베이스]\n"
    "아래는 이 워크스페이스에서 조사·종합된 자료입니다. 초안의 사실 근거로 활용하세요. "
    "(`=== ... ===` 구분선과 `[doc_*]` 인용 마커는 근거 추적용이며, 최종 문서 본문에는 노출하지 마세요.)\n\n"
    "{knowledge}"
)

DRAFT_NO_KNOWLEDGE_NOTICE = (
    "[지식베이스]\n"
    "이 워크스페이스에는 아직 조사로 수집된 지식베이스가 없습니다. 제공된 문서 유형·목차·핵심 내용만으로 "
    "골격 초안을 작성하되, 구체적 사실이 필요한 자리는 채울 내용을 표시하는 자리(예: [구체 수치 기입])로 남기세요."
)


# Knowledge base goes FIRST (primacy) so the model treats it as the source
# material, then the spec + a closing instruction that points back at it
# (recency). {audience_block} / {keypoints_block} are "" or a leading-newline
# block so the template stays clean when those optional fields are empty.
DRAFT_USER_PROMPT_TEMPLATE = """{knowledge_block}

────────────────────
위 [지식베이스]의 내용을 근거로 다음 사양에 맞는 한국어 초안을 작성해 주세요.

[문서 유형] {doc_type}
[톤 앤 매너] {tone_guide}
[분량] {length_guide}{audience_block}{keypoints_block}

[목차]
{outline}

[작성 지침]
- 각 목차 항목을 `## 항목명` 제목으로 두고, 그 아래에 [지식베이스]의 구체적 사실·수치·내용을 반영한 본문을 작성하세요.
- 양식 설명이나 빈 골격이 아니라, 조사된 실제 내용으로 채워진 완성 문서를 작성하세요.
- 문서 맨 위에 `# 제목` 한 줄을 작성하세요."""


# --- uploaded-form (양식 파일 사용) path -------------------------------------
# When the user uploads a form file, its extracted structure (headings /
# bullets / tables, body stripped) becomes a Markdown *template* the draft must
# follow. The block + template-specific user prompt below replace the built-in
# outline-only flow for that path.
DRAFT_TEMPLATE_BLOCK_TEMPLATE = (
    "[양식 템플릿]\n"
    "아래는 사용자가 업로드한 문서에서 추출한 양식 구조입니다. 이 제목 계층·글머리표·표 등 "
    "특수 요소를 그대로 따르고, 비어 있는 부분을 [지식베이스]의 내용으로 채워 완성하세요.\n\n"
    "{template}"
)

DRAFT_USER_PROMPT_TEMPLATE_TEMPLATED = """{knowledge_block}

{template_block}

────────────────────
위 [지식베이스]를 근거로, [양식 템플릿]의 구조를 따르는 한국어 초안을 작성해 주세요.

[문서 유형] {doc_type}
[톤 앤 매너] {tone_guide}
[분량] {length_guide}{audience_block}{keypoints_block}

[목차] (이 순서·구성으로 작성)
{outline}

[작성 지침]
- [목차]의 각 항목을 그 순서대로 작성하고, 각 항목에 [양식 템플릿]의 해당 제목 계층·글머리표·표 구조를 반영하세요.
- 각 부분을 [지식베이스]의 구체적 사실·수치·내용으로 채워 완성하세요. 표는 칸 구조를 유지한 채 채우되, 근거가 없는 칸은 비워 둡니다.
- 양식 설명이나 빈 골격이 아니라, 조사된 실제 내용으로 채워진 완성 문서를 작성하세요.
- 문서 맨 위에 `# 제목` 한 줄을 작성하세요."""


__all__ = [
    "DRAFT_SYSTEM_PROMPT",
    "DRAFT_TONE_GUIDE",
    "DRAFT_LENGTH_GUIDE",
    "DRAFT_KNOWLEDGE_BLOCK_TEMPLATE",
    "DRAFT_NO_KNOWLEDGE_NOTICE",
    "DRAFT_USER_PROMPT_TEMPLATE",
    "DRAFT_TEMPLATE_BLOCK_TEMPLATE",
    "DRAFT_USER_PROMPT_TEMPLATE_TEMPLATED",
]
