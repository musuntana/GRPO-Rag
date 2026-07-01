from ragos_api.routes.kb import router as kb_router
from ragos_api.routes.model_bindings import router as model_bindings_router
from ragos_api.routes.model_center import router as model_center_router
from ragos_api.routes.platform_users import router as platform_users_router
from ragos_api.routes.search import router as search_router
from ragos_api.routes.tasks import router as tasks_router
from ragos_api.routes.tenant import router as tenant_router
from ragos_api.routes.users import router as users_router

__all__ = [
    "kb_router",
    "model_bindings_router",
    "model_center_router",
    "platform_users_router",
    "search_router",
    "tasks_router",
    "tenant_router",
    "users_router",
]
