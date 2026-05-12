from .dashboard import router as dashboard_router
from .document_assist import router as document_assist_router
from .documents import router as documents_router
from .draft_chat import router as draft_chat_router
from .feedback import router as feedback_router
from .frontend import router as frontend_router
from .research import router as research_router
from .settings import router as settings_router
from .system import router as system_router
from .verify import router as verify_router
from .workspaces import router as workspaces_router
from .write import router as write_router

__all__ = [
    "dashboard_router",
    "document_assist_router",
    "documents_router",
    "draft_chat_router",
    "feedback_router",
    "frontend_router",
    "research_router",
    "settings_router",
    "system_router",
    "verify_router",
    "workspaces_router",
    "write_router",
]
