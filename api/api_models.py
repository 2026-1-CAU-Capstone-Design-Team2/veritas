from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class WorkspaceSwitchRequest(BaseModel):
    workspaceId: str


class DraftGenerateRequest(BaseModel):
    workspaceId: str
    prompt: str


class DraftRegenerateRequest(BaseModel):
    prompt: str


class DraftFormSelection(BaseModel):
    key: str = ""
    label: str = ""


class DraftBuiltinGenerateRequest(BaseModel):
    """Structured settings for a built-in form draft ("이 구성으로 초안 생성").

    Unlike ``DraftGenerateRequest`` (a flat text prompt), this carries the full
    wizard configuration so the backend can map the tone to a sampling strategy
    and persist the settings for later regeneration. ``source`` is "custom" for
    a built-in form; "file" is reserved for the uploaded-form mode.
    """

    workspaceId: str
    source: Literal["custom", "file"] = "custom"
    category: DraftFormSelection | None = None
    subtype: DraftFormSelection | None = None
    outline: list[str] = Field(default_factory=list)
    tone: str = "중립"
    length: str = "보통"
    audience: str = ""
    keyPoints: str = ""
    # Extracted Markdown template from an uploaded form file (source="file").
    # When present, generation follows this structure (headings / tables) rather
    # than the outline alone. Empty for the built-in form path.
    formMarkdown: str = ""
    # Documents the user kept checked in the draft wizard's "자료 선택" step
    # (sourced from the verification results, keyed by ``docId``). ``None`` means
    # the step was skipped (no filter — ground on the full knowledge base); a
    # list — even an empty one — means the user made an explicit selection, so
    # generation grounds *only* on those documents' per-doc summaries.
    selectedDocIds: list[str] | None = None


class DraftBuiltinRegenerateRequest(BaseModel):
    workspaceId: str
    draftNumber: int = Field(ge=1)


class ChatMessageRequest(BaseModel):
    workspaceId: str
    message: str
    mode: Literal["research", "autosurvey", "rag"] = "research"
    # Optional editor-surface extras. Defaults keep the main chat request
    # unchanged; the editor's 문서 대화 sends the open document as additive
    # context and tags its turns so both surfaces share one chat log.
    docText: str = ""
    source: str = "chat"


class FeedbackAnalyzeRequest(BaseModel):
    fileIds: list[str]


class ResearchJobCreateRequest(BaseModel):
    workspaceId: str | None = None
    instruction: str
    referenceUrls: list[str] = Field(default_factory=list)
    # How many documents AutoSurvey should collect. When omitted the runtime
    # falls back to the VERITAS_MAX_DOCS environment default (15).
    maxDocs: int | None = Field(default=None, ge=1, le=50)
    # AutoSurvey pacing from 설정 > 고급 설정 > 조사 진행 방식: the initial scout
    # sample size and the per-plan collect/batch-summary cycle size. When
    # omitted the runtime falls back to the VERITAS_SCOUT_DOCS / VERITAS_BATCH_SIZE
    # environment defaults (3 / 5).
    scoutDocs: int | None = Field(default=None, ge=1, le=50)
    collectBatchSize: int | None = Field(default=None, ge=1, le=9999)


class DocumentAssistAnalyzeRequest(BaseModel):
    workspaceId: str
    text: str
    cursor: int | None = None


class DocumentAssistChatRequest(BaseModel):
    workspaceId: str
    message: str
    mode: Literal["research", "autosurvey", "rag"] = "research"


class EditorSuggestRequest(BaseModel):
    # Inline ghost-writing context. `prefix` is the ~500 chars before the
    # cursor, `suffix` a little after; the suggestion continues at the cursor.
    workspaceId: str
    prefix: str = ""
    suffix: str = ""
    maxTokens: int = Field(default=64, ge=8, le=256)
    useWorkspace: bool = True


class EditorSaveRequest(BaseModel):
    workspaceId: str
    docId: str
    content: str
    title: str | None = None


class EditorExportRequest(BaseModel):
    workspaceId: str
    docId: str | None = None
    content: str
    format: Literal["docx", "pdf", "html", "md"]
    outputPath: str


class EditorAssistRequest(BaseModel):
    workspaceId: str
    action: Literal["rewrite", "summarize", "polish", "grammar", "continue"]
    text: str = ""
    maxTokens: int = Field(default=400, ge=16, le=1024)
    useWorkspace: bool = True


class SettingsModelRequest(BaseModel):
    modelId: str | None = None
    # Kept for old frontend payloads. New settings store selected GGUF models
    # by stable catalog id, not by display text.
    modelName: str | None = None


class SettingsEmbeddingModelRequest(BaseModel):
    modelId: str


class SettingsLocalAccessRequest(BaseModel):
    folderPaths: list[str] = Field(default_factory=list)


class DocumentToolItem(BaseModel):
    name: str
    identifier: str = ""


class SettingsDocumentToolsRequest(BaseModel):
    customTools: list[DocumentToolItem] = Field(default_factory=list)


class SettingsResearchMethodRequest(BaseModel):
    # 최초 샘플링 개수 (scout_docs) / 각 플랜당 조사 개수 (collect_batch_size).
    # Bounds are lenient — the settings UI enforces the practical range; this
    # only guards against garbage input.
    sampleCount: int = Field(default=3, ge=1, le=50)
    planCount: int = Field(default=5, ge=1, le=9999)


class SettingsLlmParallelRequest(BaseModel):
    # 병렬 디코딩 동시 요청 수 (LLMClient.max_parallel). Matches llama-server's
    # -np slot count. 1 = serial. Hard-bounded 1..5 to match the settings UI
    # stepper and to keep low-spec machines from oversubscribing the server.
    value: int = Field(default=1, ge=1, le=5)


class TypingContextRequest(BaseModel):
    sessionId: str
    workspaceId: str
    cursor: int
    prefix: str
    suffix: str


class PredictionAckRequest(BaseModel):
    action: Literal["accept", "dismiss"]


class NavigateRequest(BaseModel):
    route: str


class WorkspaceSyncRequest(BaseModel):
    workspaceId: str
    workspaceName: str


class ToastRequest(BaseModel):
    level: Literal["info", "success", "warning", "error"]
    message: str


class PredictionShowRequest(BaseModel):
    predictionId: str
    text: str
    confidence: float
    anchor: str


class PredictionHideRequest(BaseModel):
    predictionId: str
    reason: str


class PredictionApplyRequest(BaseModel):
    predictionId: str
    insertMode: str


class ScreenMonitoringStartRequest(BaseModel):
    workspaceId: str | None = None
