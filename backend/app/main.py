"""FastAPI application entrypoint and router wiring for the backend."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi_pagination import add_pagination

from app.api.activity import router as activity_router
from app.api.agent import router as agent_router
from app.api.agents import router as agents_router
from app.api.approvals import router as approvals_router
from app.api.chat import router as chat_router
from app.api.openclaw_ui_proxy import router as openclaw_ui_proxy_router
from app.api.auth import router as auth_router
from app.api.board_group_memory import router as board_group_memory_router
from app.api.board_groups import router as board_groups_router
from app.api.board_memory import router as board_memory_router
from app.api.board_onboarding import router as board_onboarding_router
from app.api.board_webhooks import router as board_webhooks_router
from app.api.backups import router as backups_router
from app.api.boards import router as boards_router
from app.api.board_chat import router as board_chat_router
from app.api.calendar import router as calendar_router
from app.api.memory import router as memory_router
from app.api.pev import router as pev_router
from app.api.projects import router as projects_router
from app.api.gateway import router as gateway_router
from app.api.agent_messages import router as agent_messages_router
from app.api.board_skill_allowlist import router as board_skill_allowlist_router
from app.api.gateways import router as gateways_router
from app.api.metrics import router as metrics_router
from app.api.organizations import router as organizations_router
from app.api.skills_marketplace import router as skills_marketplace_router
from app.api.souls_directory import router as souls_directory_router
from app.api.tags import router as tags_router
from app.api.task_custom_fields import router as task_custom_fields_router
from app.api.task_attachments import router as task_attachments_router
from app.api.roles import router as roles_router
from app.api.tasks import router as tasks_router
from app.api.office import router as office_router
from app.api.pipeline import router as pipeline_router
from app.api.team import router as team_router
# T103 v6 (2026-05-18): proxy_generic router DISABLED — agents route via
# OmniRoute now (T103). Import kept for any in-tree helper reuse.
# from app.api.proxy_generic import router as proxy_generic_router
from app.api.proxy_ollama import router as proxy_ollama_router
# T103 v6 (2026-05-18): proxy_vllm router DISABLED — agents route via
# OmniRoute now. Import kept for in-tree helper reuse.
# from app.api.proxy_vllm import router as proxy_vllm_router
from app.api.model_ranking import router as model_ranking_router
from app.api.installed_skills import router as installed_skills_router
from app.api.provider_capacity import router as provider_capacity_router
from app.api.provider_stats import router as provider_stats_router
from app.api.omniroute_webhook import (
    router as omniroute_webhook_router,
    ensure_omniroute_webhook_registered,
)
from app.api.integrations import router as integrations_router
from app.api.runtime_settings_api import router as runtime_settings_router
from app.api.review import router as review_router
from app.api.router import router as router_router
from app.api.tests import router as tests_router
from app.api.kb import router as kb_router
from app.api.votes import router as votes_router
from app.api.traffic import router as traffic_router
from app.api.wiki import router as wiki_router
from app.api.health import router as health_router
from app.api.users import router as users_router
from app.core.config import settings
from app.core.error_handling import install_error_handling
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import validate_rate_limit_redis
from app.core.rate_limit_backend import RateLimitBackend
from app.core.security_headers import SecurityHeadersMiddleware
from app.db.session import init_db
from app.schemas.health import HealthStatusResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

configure_logging()
logger = get_logger(__name__)
OPENAPI_TAGS = [
    {
        "name": "auth",
        "description": (
            "Authentication bootstrap endpoints for resolving caller identity and session context."
        ),
    },
    {
        "name": "health",
        "description": (
            "Service liveness/readiness probes used by infrastructure and runtime checks."
        ),
    },
    {
        "name": "agents",
        "description": "Organization-level agent directory, lifecycle, and management operations.",
    },
    {
        "name": "activity",
        "description": "Activity feed and audit timeline endpoints across boards and operations.",
    },
    {
        "name": "gateways",
        "description": "Gateway management, synchronization, and runtime control operations.",
    },
    {
        "name": "metrics",
        "description": "Aggregated operational and board analytics metrics endpoints.",
    },
    {
        "name": "organizations",
        "description": "Organization profile, membership, and governance management endpoints.",
    },
    {
        "name": "souls-directory",
        "description": "Directory and lookup endpoints for agent soul templates and variants.",
    },
    {
        "name": "skills",
        "description": "Skills marketplace, install, uninstall, and synchronization endpoints.",
    },
    {
        "name": "board-groups",
        "description": "Board group CRUD, assignment, and grouping workflow endpoints.",
    },
    {
        "name": "board-group-memory",
        "description": "Shared memory endpoints scoped to board groups and grouped boards.",
    },
    {
        "name": "boards",
        "description": "Board lifecycle, configuration, and board-level management endpoints.",
    },
    {
        "name": "board-memory",
        "description": "Board-scoped memory read/write endpoints for persistent context.",
    },
    {
        "name": "board-webhooks",
        "description": "Board webhook registration, delivery config, and lifecycle endpoints.",
    },
    {
        "name": "board-onboarding",
        "description": "Board onboarding state, setup actions, and onboarding workflow endpoints.",
    },
    {
        "name": "approvals",
        "description": "Approval request, review, and status-tracking operations for board tasks.",
    },
    {
        "name": "tasks",
        "description": "Task CRUD, dependency management, and task workflow operations.",
    },
    {
        "name": "custom-fields",
        "description": "Organization custom-field definitions and board assignment endpoints.",
    },
    {
        "name": "tags",
        "description": "Tag catalog and task-tag association management endpoints.",
    },
    {
        "name": "users",
        "description": "User profile read/update operations and user-centric settings endpoints.",
    },
    {
        "name": "agent",
        "description": (
            "Agent-scoped API surface. All endpoints require `X-Agent-Token` and are "
            "constrained by agent board access policies."
        ),
    },
    {
        "name": "agent-lead",
        "description": (
            "Lead workflows: delegation, review orchestration, approvals, and "
            "coordination actions."
        ),
    },
    {
        "name": "agent-worker",
        "description": (
            "Worker workflows: task execution, task comments, and board/group context "
            "reads/writes used during heartbeat loops."
        ),
    },
    {
        "name": "agent-main",
        "description": (
            "Gateway-main control workflows that message board leads or broadcast "
            "coordination requests."
        ),
    },
]

_JSON_SCHEMA_REF_PREFIX = "#/components/schemas/"
_OPENAPI_EXAMPLE_TAGS = {
    "agents",
    "activity",
    "gateways",
    "metrics",
    "organizations",
    "souls-directory",
    "skills",
    "board-groups",
    "board-group-memory",
    "boards",
    "board-memory",
    "board-webhooks",
    "board-onboarding",
    "approvals",
    "tasks",
    "custom-fields",
    "tags",
    "users",
}
_GENERIC_RESPONSE_DESCRIPTIONS = {"Successful Response", "Validation Error"}
_HTTP_RESPONSE_DESCRIPTIONS = {
    "200": "Request completed successfully.",
    "201": "Resource created successfully.",
    "202": "Request accepted for processing.",
    "204": "Request completed successfully with no response body.",
    "400": "Request validation failed.",
    "401": "Authentication is required or token is invalid.",
    "403": "Caller is authenticated but not authorized for this operation.",
    "404": "Requested resource was not found.",
    "409": "Request conflicts with the current resource state.",
    "422": "Request payload failed schema or field validation.",
    "429": "Request was rate-limited.",
    "500": "Internal server error.",
}
_METHOD_SUMMARY_PREFIX = {
    "get": "List",
    "post": "Create",
    "put": "Replace",
    "patch": "Update",
    "delete": "Delete",
}


def _resolve_schema_ref(
    schema: dict[str, Any],
    *,
    components: dict[str, Any],
    seen_refs: set[str] | None = None,
) -> dict[str, Any]:
    """Resolve local component refs for OpenAPI schema traversal."""
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    if not ref.startswith(_JSON_SCHEMA_REF_PREFIX):
        return schema
    if seen_refs is None:
        seen_refs = set()
    if ref in seen_refs:
        return schema
    seen_refs.add(ref)
    schema_name = ref[len(_JSON_SCHEMA_REF_PREFIX) :]
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return schema
    target = schemas.get(schema_name)
    if not isinstance(target, dict):
        return schema
    return _resolve_schema_ref(target, components=components, seen_refs=seen_refs)


def _example_from_schema(schema: dict[str, Any], *, components: dict[str, Any]) -> Any:
    """Generate an OpenAPI example from schema metadata with sensible fallbacks."""
    resolved = _resolve_schema_ref(schema, components=components)

    if "example" in resolved:
        return resolved["example"]
    examples = resolved.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0]

    for composite_key in ("anyOf", "oneOf", "allOf"):
        composite = resolved.get(composite_key)
        if isinstance(composite, list):
            for branch in composite:
                if not isinstance(branch, dict):
                    continue
                branch_example = _example_from_schema(branch, components=components)
                if branch_example is not None:
                    return branch_example

    enum_values = resolved.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]

    schema_type = resolved.get("type")
    if schema_type == "object":
        output: dict[str, Any] = {}
        properties = resolved.get("properties")
        if isinstance(properties, dict):
            for key, property_schema in properties.items():
                if not isinstance(property_schema, dict):
                    continue
                property_example = _example_from_schema(
                    property_schema, components=components
                )
                if property_example is not None:
                    output[key] = property_example
        if output:
            return output
        additional_properties = resolved.get("additionalProperties")
        if isinstance(additional_properties, dict):
            value_example = _example_from_schema(
                additional_properties, components=components
            )
            if value_example is not None:
                return {"key": value_example}
        return {}

    if schema_type == "array":
        items = resolved.get("items")
        if isinstance(items, dict):
            item_example = _example_from_schema(items, components=components)
            if item_example is not None:
                return [item_example]
        return []

    if schema_type == "string":
        return "string"
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0
    if schema_type == "boolean":
        return False

    return None


def _inject_json_content_example(
    *,
    content: dict[str, Any],
    components: dict[str, Any],
) -> None:
    """Attach an example to application/json content when one is missing."""
    app_json = content.get("application/json")
    if not isinstance(app_json, dict):
        return
    if "example" in app_json or "examples" in app_json:
        return
    schema = app_json.get("schema")
    if not isinstance(schema, dict):
        return
    generated_example = _example_from_schema(schema, components=components)
    if generated_example is not None:
        app_json["example"] = generated_example


def _build_operation_summary(*, method: str, path: str) -> str:
    """Build a readable summary when an operation does not define one."""
    prefix = _METHOD_SUMMARY_PREFIX.get(method.lower(), "Handle")
    path_without_prefix = path.removeprefix("/api/v1/")
    parts = [
        part.replace("-", " ")
        for part in path_without_prefix.split("/")
        if part and not (part.startswith("{") and part.endswith("}"))
    ]
    if not parts:
        return prefix
    return f"{prefix} {' '.join(parts)}".strip().title()


def _normalize_operation_docs(
    *,
    operation: dict[str, Any],
    method: str,
    path: str,
) -> None:
    """Normalize summary/description/responses/request-body docs for tagged operations."""
    summary = str(operation.get("summary", "")).strip()
    if not summary:
        summary = _build_operation_summary(method=method, path=path)
        operation["summary"] = summary

    description = str(operation.get("description", "")).strip()
    if not description:
        operation["description"] = f"{summary}."

    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        if not str(request_body.get("description", "")).strip():
            request_body["description"] = "JSON request payload."

    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return
    for status_code, response in responses.items():
        if not isinstance(response, dict):
            continue
        existing_description = str(response.get("description", "")).strip()
        if (
            not existing_description
            or existing_description in _GENERIC_RESPONSE_DESCRIPTIONS
        ):
            response["description"] = _HTTP_RESPONSE_DESCRIPTIONS.get(
                str(status_code),
                "Request processed.",
            )


def _inject_tagged_operation_openapi_docs(openapi_schema: dict[str, Any]) -> None:
    """Ensure targeted-tag operations expose consistent OpenAPI docs and examples."""
    components = openapi_schema.get("components")
    if not isinstance(components, dict):
        return
    paths = openapi_schema.get("paths")
    if not isinstance(paths, dict):
        return

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if not isinstance(operation, dict):
                continue
            tags = operation.get("tags")
            if not isinstance(tags, list):
                continue
            if not _OPENAPI_EXAMPLE_TAGS.intersection(tags):
                continue

            _normalize_operation_docs(operation=operation, method=method, path=path)

            request_body = operation.get("requestBody")
            if isinstance(request_body, dict):
                request_content = request_body.get("content")
                if isinstance(request_content, dict):
                    _inject_json_content_example(
                        content=request_content, components=components
                    )

            responses = operation.get("responses")
            if isinstance(responses, dict):
                for response in responses.values():
                    if not isinstance(response, dict):
                        continue
                    response_content = response.get("content")
                    if isinstance(response_content, dict):
                        _inject_json_content_example(
                            content=response_content, components=components
                        )


def _build_custom_openapi(fastapi_app: FastAPI) -> dict[str, Any]:
    """Generate OpenAPI schema with normalized docs/examples for targeted tags."""
    if fastapi_app.openapi_schema:
        return fastapi_app.openapi_schema
    openapi_schema = get_openapi(
        title=fastapi_app.title,
        version=fastapi_app.version,
        openapi_version=fastapi_app.openapi_version,
        description=fastapi_app.description,
        routes=fastapi_app.routes,
        tags=fastapi_app.openapi_tags,
        servers=fastapi_app.servers,
    )
    _inject_tagged_operation_openapi_docs(openapi_schema)
    fastapi_app.openapi_schema = openapi_schema
    return fastapi_app.openapi_schema


class MissionControlFastAPI(FastAPI):
    """FastAPI application with custom OpenAPI normalization."""

    def openapi(self) -> dict[str, Any]:
        return _build_custom_openapi(self)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Initialize application resources before serving requests."""
    import asyncio
    from app.services.watchdog_loop import repeat_reset_loop, watchdog_loop

    logger.info(
        "app.lifecycle.starting environment=%s db_auto_migrate=%s",
        settings.environment,
        settings.db_auto_migrate,
    )
    await init_db()
    if settings.rate_limit_backend == RateLimitBackend.REDIS:
        validate_rate_limit_redis(settings.rate_limit_redis_url)
        logger.info("app.lifecycle.rate_limit backend=redis")
    else:
        logger.info("app.lifecycle.rate_limit backend=memory")

    # Start watchdog auto-loop in background. Ignored if WATCHDOG_ENABLED=false.
    watchdog_task = asyncio.create_task(watchdog_loop(), name="watchdog_loop")
    # T66 (2026-05-15): dedicated 30s tick for `_process_interval_repeats`
    # so short-interval repeat tasks (e.g. 5-min) get reset within ≤30s
    # of the interval boundary instead of waiting for the main 180s tick.
    repeat_reset_task = asyncio.create_task(
        repeat_reset_loop(), name="watchdog_repeat_reset_loop",
    )
    # T76 (2026-05-16): 30-min provider benchmark loop. Fires "why is the sky
    # blue" at every enabled (provider, model), records TPS, auto-disables
    # models with 3+ failures in 5 min, flips providers offline when all
    # their models become disabled.
    from app.services.provider_benchmark import benchmark_loop
    benchmark_task = asyncio.create_task(benchmark_loop(), name="provider_benchmark_loop")
    # T103 v6 (2026-05-18): traffic ETL mirror — polls OmniRoute
    # /api/usage/call-logs into llm_flows.db so /traffic UI shows all
    # gateway inference (post-T103 agents skip MC entirely).
    from app.services.traffic_etl import traffic_etl_loop
    traffic_etl_task = asyncio.create_task(traffic_etl_loop(), name="traffic_etl_loop")
    # T103 v16 (2026-05-19): register an OmniRoute webhook so OR→MC sync
    # becomes event-driven. The watchdog still polls every ~10 min as a
    # failsafe (see omniroute_sync.reverse_sync_to_mc_db). Best-effort:
    # OR unreachable at boot = log + continue, next boot will retry.
    try:
        await ensure_omniroute_webhook_registered()
    except Exception as exc:
        logger.warning("omniroute_webhook.bootstrap_failed err=%s", exc)
    logger.info("app.lifecycle.started")
    try:
        yield
    finally:
        for t in (watchdog_task, repeat_reset_task, benchmark_task, traffic_etl_task):
            t.cancel()
        for t in (watchdog_task, repeat_reset_task, benchmark_task, traffic_etl_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("app.lifecycle.stopped")


app = MissionControlFastAPI(
    title="Mission Control API",
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAGS,
)

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Total-Count", "X-Limit", "X-Offset"],
    )
    logger.info("app.cors.enabled origins_count=%s", len(origins))
else:
    logger.info("app.cors.disabled")

app.add_middleware(
    SecurityHeadersMiddleware,
    x_content_type_options=settings.security_header_x_content_type_options,
    x_frame_options=settings.security_header_x_frame_options,
    referrer_policy=settings.security_header_referrer_policy,
    permissions_policy=settings.security_header_permissions_policy,
)
install_error_handling(app)


@app.get(
    "/health",
    tags=["health"],
    response_model=HealthStatusResponse,
    summary="Health Check",
    description="Lightweight liveness probe endpoint.",
    responses={
        status.HTTP_200_OK: {
            "description": "Service is alive.",
            "content": {"application/json": {"example": {"ok": True}}},
        }
    },
)
def health() -> HealthStatusResponse:
    """Lightweight liveness probe endpoint."""
    return HealthStatusResponse(ok=True)


@app.get(
    "/healthz",
    tags=["health"],
    response_model=HealthStatusResponse,
    summary="Health Alias Check",
    description="Alias liveness probe endpoint for platform compatibility.",
    responses={
        status.HTTP_200_OK: {
            "description": "Service is alive.",
            "content": {"application/json": {"example": {"ok": True}}},
        }
    },
)
def healthz() -> HealthStatusResponse:
    """Alias liveness probe endpoint for platform compatibility."""
    return HealthStatusResponse(ok=True)


@app.get(
    "/readyz",
    tags=["health"],
    response_model=HealthStatusResponse,
    summary="Readiness Check",
    description="Readiness probe endpoint for service orchestration checks.",
    responses={
        status.HTTP_200_OK: {
            "description": "Service is ready.",
            "content": {"application/json": {"example": {"ok": True}}},
        }
    },
)
def readyz() -> HealthStatusResponse:
    """Readiness probe endpoint for service orchestration checks."""
    return HealthStatusResponse(ok=True)


api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(auth_router)
api_v1.include_router(agent_router)
api_v1.include_router(agents_router)
api_v1.include_router(agent_messages_router)
api_v1.include_router(board_skill_allowlist_router)
api_v1.include_router(activity_router)
api_v1.include_router(gateway_router)
api_v1.include_router(gateways_router)
api_v1.include_router(metrics_router)
api_v1.include_router(organizations_router)
api_v1.include_router(souls_directory_router)
api_v1.include_router(skills_marketplace_router)
api_v1.include_router(board_groups_router)
api_v1.include_router(board_group_memory_router)
api_v1.include_router(backups_router)
api_v1.include_router(boards_router)
api_v1.include_router(calendar_router)
api_v1.include_router(memory_router)
api_v1.include_router(pev_router)
api_v1.include_router(projects_router)
api_v1.include_router(board_memory_router)
api_v1.include_router(board_webhooks_router)
api_v1.include_router(board_onboarding_router)
api_v1.include_router(approvals_router)
api_v1.include_router(chat_router)
api_v1.include_router(roles_router)
api_v1.include_router(tasks_router)
api_v1.include_router(board_chat_router)
api_v1.include_router(task_attachments_router)
api_v1.include_router(task_custom_fields_router)
api_v1.include_router(tags_router)
api_v1.include_router(office_router)
api_v1.include_router(pipeline_router)
api_v1.include_router(team_router)
api_v1.include_router(proxy_ollama_router)
# T103 v6 (2026-05-18): proxy_vllm + proxy_generic disabled — agents use
# OmniRoute as single egress. Routers removed from /api/v1; .py files
# kept for in-tree helper reuse.
# api_v1.include_router(proxy_vllm_router)
# api_v1.include_router(proxy_generic_router)
api_v1.include_router(provider_capacity_router)
api_v1.include_router(model_ranking_router)
api_v1.include_router(installed_skills_router)
api_v1.include_router(provider_stats_router)
api_v1.include_router(omniroute_webhook_router)
api_v1.include_router(runtime_settings_router)
api_v1.include_router(integrations_router)
api_v1.include_router(review_router)
api_v1.include_router(router_router)
api_v1.include_router(tests_router)
api_v1.include_router(kb_router)
api_v1.include_router(votes_router)
api_v1.include_router(traffic_router)
api_v1.include_router(wiki_router)
api_v1.include_router(health_router)
api_v1.include_router(users_router)
app.include_router(api_v1)
# T88b (2026-05-16): root-level reverse proxy for the openclaw-gateway
# web UI so the /openclaw tab can embed it (gateway sends XFO+CSP that
# block iframes; we strip those + inject `<base>` for relative paths).
app.include_router(openclaw_ui_proxy_router)

add_pagination(app)
logger.debug("app.routes.registered count=%s", len(app.routes))
