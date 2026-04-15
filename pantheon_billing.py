from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import text

import orchestrator as orch
import pantheon_storage as storage

try:
    import stripe
except Exception:  # pragma: no cover - optional at import time
    stripe = None


ACCOUNT_STATUS_ACTIVE = "active"
ACCOUNT_STATUS_INACTIVE = "inactive"
ACTIVE_SUBSCRIPTION_STATUSES = {"active"}
DEFAULT_CREDIT_VALUE_MICRO_USD = 4_000
DEFAULT_MARGIN_BASIS_POINTS = 25_000
DEFAULT_CHARS_PER_TOKEN = 2.0
DEFAULT_DRY_RUN_ENABLED = False
DEFAULT_DRY_RUN_CREDITS = 100
DEFAULT_MODEL_COSTS = {
    "openai:gpt-5-mini": {
        "input_cost_per_1k_tokens_microusd": 5_000,
        "output_cost_per_1k_tokens_microusd": 20_000,
    },
    "openai:gpt-5.4": {
        "input_cost_per_1k_tokens_microusd": 15_000,
        "output_cost_per_1k_tokens_microusd": 60_000,
    },
    "anthropic:claude-haiku-4-5": {
        "input_cost_per_1k_tokens_microusd": 1_000,
        "output_cost_per_1k_tokens_microusd": 5_000,
    },
    "anthropic:claude-sonnet-4-6": {
        "input_cost_per_1k_tokens_microusd": 6_000,
        "output_cost_per_1k_tokens_microusd": 30_000,
    },
    "anthropic:claude-opus-4-6": {
        "input_cost_per_1k_tokens_microusd": 18_000,
        "output_cost_per_1k_tokens_microusd": 90_000,
    },
    "gemini:gemini-2.5-flash": {
        "input_cost_per_1k_tokens_microusd": 500,
        "output_cost_per_1k_tokens_microusd": 2_000,
    },
    "gemini:gemini-2.5-pro": {
        "input_cost_per_1k_tokens_microusd": 4_000,
        "output_cost_per_1k_tokens_microusd": 15_000,
    },
    "xai:grok-3-mini": {
        "input_cost_per_1k_tokens_microusd": 3_000,
        "output_cost_per_1k_tokens_microusd": 12_000,
    },
    "xai:grok-4": {
        "input_cost_per_1k_tokens_microusd": 12_000,
        "output_cost_per_1k_tokens_microusd": 50_000,
    },
}


@dataclass(frozen=True)
class PlanBlueprint:
    plan_id: str
    name: str
    description: str
    plan_type: str
    monthly_price_cents: int
    included_credits: int
    stripe_price_env: str
    model_access: List[str]
    display_order: int


DEFAULT_PLAN_BLUEPRINTS = [
    PlanBlueprint(
        plan_id="starter-monthly",
        name="Starter",
        description="Essential access for smaller prompts and lighter runs.",
        plan_type="subscription",
        monthly_price_cents=1_500,
        included_credits=3_000,
        stripe_price_env="STRIPE_PRICE_STARTER_MONTHLY",
        model_access=[
            "openai:gpt-5-mini",
            "anthropic:claude-haiku-4-5",
            "gemini:gemini-2.5-flash",
            "xai:grok-3-mini",
        ],
        display_order=1,
    ),
    PlanBlueprint(
        plan_id="pro-monthly",
        name="Pro",
        description="Broader model access for heavier collaborative runs.",
        plan_type="subscription",
        monthly_price_cents=3_000,
        included_credits=7_500,
        stripe_price_env="STRIPE_PRICE_PRO_MONTHLY",
        model_access=[
            "openai:gpt-5-mini",
            "openai:gpt-5.4",
            "anthropic:claude-haiku-4-5",
            "anthropic:claude-sonnet-4-6",
            "gemini:gemini-2.5-flash",
            "gemini:gemini-2.5-pro",
            "xai:grok-3-mini",
        ],
        display_order=2,
    ),
    PlanBlueprint(
        plan_id="scale-monthly",
        name="Scale",
        description="Full Pantheon access for larger teams and high-volume work.",
        plan_type="subscription",
        monthly_price_cents=10_000,
        included_credits=30_000,
        stripe_price_env="STRIPE_PRICE_SCALE_MONTHLY",
        model_access=[
            "openai:gpt-5-mini",
            "openai:gpt-5.4",
            "anthropic:claude-haiku-4-5",
            "anthropic:claude-sonnet-4-6",
            "anthropic:claude-opus-4-6",
            "gemini:gemini-2.5-flash",
            "gemini:gemini-2.5-pro",
            "xai:grok-3-mini",
            "xai:grok-4",
        ],
        display_order=3,
    ),
    PlanBlueprint(
        plan_id="credits-2500",
        name="Credit Pack 2,500",
        description="One-time prepaid credits for overage usage.",
        plan_type="credit_pack",
        monthly_price_cents=1_200,
        included_credits=2_500,
        stripe_price_env="STRIPE_PRICE_CREDITS_2500",
        model_access=[],
        display_order=11,
    ),
    PlanBlueprint(
        plan_id="credits-8000",
        name="Credit Pack 8,000",
        description="One-time prepaid credits for larger overage needs.",
        plan_type="credit_pack",
        monthly_price_cents=3_600,
        included_credits=8_000,
        stripe_price_env="STRIPE_PRICE_CREDITS_8000",
        model_access=[],
        display_order=12,
    ),
]


def _iso_now() -> str:
    return datetime.now().isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=True, sort_keys=True)


def _bool_env(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def credit_value_micro_usd() -> int:
    raw = str(os.environ.get("PANTHEON_CREDIT_VALUE_MICRO_USD", "")).strip()
    return int(raw or DEFAULT_CREDIT_VALUE_MICRO_USD)


def margin_basis_points() -> int:
    raw = str(os.environ.get("PANTHEON_MARGIN_BASIS_POINTS", "")).strip()
    return int(raw or DEFAULT_MARGIN_BASIS_POINTS)


def chars_per_token() -> float:
    raw = str(os.environ.get("PANTHEON_ESTIMATE_CHARS_PER_TOKEN", "")).strip()
    try:
        return max(float(raw), 1.0) if raw else DEFAULT_CHARS_PER_TOKEN
    except ValueError:
        return DEFAULT_CHARS_PER_TOKEN


def dry_run_enabled() -> bool:
    return _bool_env("PANTHEON_ENABLE_BILLED_DRY_RUN", DEFAULT_DRY_RUN_ENABLED)


def dry_run_credit_cost() -> int:
    raw = str(os.environ.get("PANTHEON_DRY_RUN_CREDITS", "")).strip()
    return int(raw or DEFAULT_DRY_RUN_CREDITS)


def stripe_secret_key() -> str:
    return str(os.environ.get("STRIPE_SECRET_KEY", "")).strip()


def stripe_webhook_secret() -> str:
    return str(os.environ.get("STRIPE_WEBHOOK_SECRET", "")).strip()


def billing_backend_ready() -> bool:
    return storage.storage_enabled()


def stripe_ready() -> bool:
    return bool(stripe and stripe_secret_key() and stripe_webhook_secret())


def stripe_checkout_ready() -> bool:
    return bool(stripe and stripe_secret_key())


def _stripe_client() -> Any:
    if stripe is None:
        raise RuntimeError("Stripe SDK is not installed on the server.")
    secret = stripe_secret_key()
    if not secret:
        raise RuntimeError("Stripe is not configured on the server.")
    stripe.api_key = secret
    return stripe


def _model_key(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def _provider_configured(provider: str) -> bool:
    return bool(orch.get_env_value(*orch.provider_env_names(provider)))


def _load_model_costs() -> Dict[str, Dict[str, Any]]:
    raw = str(os.environ.get("PANTHEON_MODEL_COSTS_JSON", "")).strip()
    payload: Dict[str, Any] = {}
    if raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - env misconfiguration
            raise RuntimeError("PANTHEON_MODEL_COSTS_JSON is invalid JSON.") from exc
        payload = decoded if isinstance(decoded, dict) else {}

    merged: Dict[str, Dict[str, Any]] = {key: dict(value) for key, value in DEFAULT_MODEL_COSTS.items()}
    for key, value in payload.items():
        if isinstance(value, dict):
            merged[str(key)] = dict(value)
    return merged


def configured_model_cost(provider: str, model: str) -> Optional[Dict[str, Any]]:
    payload = _load_model_costs().get(_model_key(provider, model))
    if not isinstance(payload, dict):
        return None
    try:
        return {
            "input_cost_per_1k_tokens_microusd": int(payload.get("input_cost_per_1k_tokens_microusd", 0)),
            "output_cost_per_1k_tokens_microusd": int(payload.get("output_cost_per_1k_tokens_microusd", 0)),
        }
    except (TypeError, ValueError):
        return None


def platform_model_available(provider: str, model: str) -> tuple[bool, str]:
    if not _provider_configured(provider):
        provider_label = orch.PROVIDER_LABELS.get(provider, provider)
        return (False, f"{provider_label} is currently unavailable.")
    if configured_model_cost(provider, model) is None:
        return (False, f"{model} does not have a bounded cost profile configured.")
    return (True, "")


def init_billing_storage() -> None:
    if not billing_backend_ready():
        return
    with storage.engine().begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": 7_145_022})

        statements = [
            """
            CREATE TABLE IF NOT EXISTS accounts (
              id VARCHAR(64) PRIMARY KEY,
              owner_user_id VARCHAR(64) NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
              status VARCHAR(32) NOT NULL DEFAULT 'inactive',
              billing_status VARCHAR(32) NOT NULL DEFAULT 'inactive',
              subscription_status VARCHAR(32) NOT NULL DEFAULT 'inactive',
              pricing_plan_id VARCHAR(64) NOT NULL DEFAULT '',
              credit_balance_credits INTEGER NOT NULL DEFAULT 0,
              created_at VARCHAR(64) NOT NULL,
              updated_at VARCHAR(64) NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS accounts_owner_user_id_idx ON accounts (owner_user_id)",
            """
            CREATE TABLE IF NOT EXISTS stripe_customers (
              account_id VARCHAR(64) PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
              stripe_customer_id VARCHAR(255) NOT NULL UNIQUE,
              email VARCHAR(320) NOT NULL DEFAULT '',
              created_at VARCHAR(64) NOT NULL,
              updated_at VARCHAR(64) NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pricing_plans (
              id VARCHAR(64) PRIMARY KEY,
              name VARCHAR(255) NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              plan_type VARCHAR(32) NOT NULL,
              monthly_price_cents INTEGER NOT NULL DEFAULT 0,
              included_credits INTEGER NOT NULL DEFAULT 0,
              stripe_price_id VARCHAR(255) NOT NULL DEFAULT '',
              model_access_json TEXT NOT NULL DEFAULT '[]',
              active BOOLEAN NOT NULL DEFAULT TRUE,
              display_order INTEGER NOT NULL DEFAULT 0,
              created_at VARCHAR(64) NOT NULL,
              updated_at VARCHAR(64) NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS pricing_plans_plan_type_idx ON pricing_plans (plan_type, active, display_order)",
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
              id SERIAL PRIMARY KEY,
              account_id VARCHAR(64) NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
              pricing_plan_id VARCHAR(64) NOT NULL DEFAULT '',
              stripe_customer_id VARCHAR(255) NOT NULL DEFAULT '',
              stripe_subscription_id VARCHAR(255) NOT NULL UNIQUE,
              stripe_price_id VARCHAR(255) NOT NULL DEFAULT '',
              status VARCHAR(32) NOT NULL DEFAULT 'inactive',
              current_period_start VARCHAR(64) NOT NULL DEFAULT '',
              current_period_end VARCHAR(64) NOT NULL DEFAULT '',
              cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
              latest_invoice_id VARCHAR(255) NOT NULL DEFAULT '',
              created_at VARCHAR(64) NOT NULL,
              updated_at VARCHAR(64) NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS subscriptions_account_id_idx ON subscriptions (account_id, status)",
            """
            CREATE TABLE IF NOT EXISTS credit_ledger (
              id SERIAL PRIMARY KEY,
              account_id VARCHAR(64) NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
              amount_credits INTEGER NOT NULL,
              balance_after_credits INTEGER NOT NULL,
              entry_type VARCHAR(32) NOT NULL,
              source_type VARCHAR(64) NOT NULL,
              source_id VARCHAR(255) NOT NULL DEFAULT '',
              stripe_event_id VARCHAR(255) NOT NULL DEFAULT '',
              description TEXT NOT NULL DEFAULT '',
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at VARCHAR(64) NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS credit_ledger_account_created_idx ON credit_ledger (account_id, id)",
            "CREATE INDEX IF NOT EXISTS credit_ledger_source_idx ON credit_ledger (source_type, source_id)",
            """
            CREATE TABLE IF NOT EXISTS usage_events (
              id VARCHAR(64) PRIMARY KEY,
              account_id VARCHAR(64) NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
              conversation_id VARCHAR(255) NOT NULL DEFAULT '',
              status VARCHAR(32) NOT NULL DEFAULT 'reserved',
              question_preview TEXT NOT NULL DEFAULT '',
              total_estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
              total_estimated_output_tokens INTEGER NOT NULL DEFAULT 0,
              max_cost_microusd BIGINT NOT NULL DEFAULT 0,
              actual_cost_microusd BIGINT NOT NULL DEFAULT 0,
              reserved_credits INTEGER NOT NULL DEFAULT 0,
              actual_credits INTEGER NOT NULL DEFAULT 0,
              refunded_credits INTEGER NOT NULL DEFAULT 0,
              participants_json TEXT NOT NULL DEFAULT '[]',
              usage_json TEXT NOT NULL DEFAULT '[]',
              created_at VARCHAR(64) NOT NULL,
              updated_at VARCHAR(64) NOT NULL,
              settled_at VARCHAR(64) NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX IF NOT EXISTS usage_events_account_id_idx ON usage_events (account_id, created_at)",
            "CREATE INDEX IF NOT EXISTS usage_events_conversation_idx ON usage_events (conversation_id)",
            """
            CREATE TABLE IF NOT EXISTS processed_webhook_events (
              stripe_event_id VARCHAR(255) PRIMARY KEY,
              event_type VARCHAR(128) NOT NULL,
              processed_at VARCHAR(64) NOT NULL,
              payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
        ]
        for statement in statements:
            connection.execute(text(statement))

        _seed_pricing_plans(connection)
        _backfill_accounts(connection)


def _seed_pricing_plans(connection: Any) -> None:
    now = _iso_now()
    for blueprint in DEFAULT_PLAN_BLUEPRINTS:
        row = connection.execute(
            text("SELECT id FROM pricing_plans WHERE id = :plan_id"),
            {"plan_id": blueprint.plan_id},
        ).mappings().first()
        params = {
            "id": blueprint.plan_id,
            "name": blueprint.name,
            "description": blueprint.description,
            "plan_type": blueprint.plan_type,
            "monthly_price_cents": blueprint.monthly_price_cents,
            "included_credits": blueprint.included_credits,
            "stripe_price_id": str(os.environ.get(blueprint.stripe_price_env, "")).strip(),
            "model_access_json": json.dumps(blueprint.model_access, ensure_ascii=True),
            "active": True,
            "display_order": blueprint.display_order,
            "created_at": now,
            "updated_at": now,
        }
        if row:
            connection.execute(
                text(
                    """
                    UPDATE pricing_plans
                    SET name = :name,
                        description = :description,
                        plan_type = :plan_type,
                        monthly_price_cents = :monthly_price_cents,
                        included_credits = :included_credits,
                        stripe_price_id = :stripe_price_id,
                        model_access_json = :model_access_json,
                        active = :active,
                        display_order = :display_order,
                        updated_at = :updated_at
                    WHERE id = :id
                    """
                ),
                params,
            )
        else:
            connection.execute(
                text(
                    """
                    INSERT INTO pricing_plans (
                      id, name, description, plan_type, monthly_price_cents, included_credits, stripe_price_id,
                      model_access_json, active, display_order, created_at, updated_at
                    ) VALUES (
                      :id, :name, :description, :plan_type, :monthly_price_cents, :included_credits, :stripe_price_id,
                      :model_access_json, :active, :display_order, :created_at, :updated_at
                    )
                    """
                ),
                params,
            )


def _backfill_accounts(connection: Any) -> None:
    rows = connection.execute(text("SELECT id FROM users")).mappings().all()
    now = _iso_now()
    for row in rows:
        existing = connection.execute(
            text("SELECT id FROM accounts WHERE owner_user_id = :user_id"),
            {"user_id": row["id"]},
        ).mappings().first()
        if existing:
            continue
        connection.execute(
            text(
                """
                INSERT INTO accounts (
                  id, owner_user_id, status, billing_status, subscription_status, pricing_plan_id,
                  credit_balance_credits, created_at, updated_at
                ) VALUES (
                  :id, :owner_user_id, :status, :billing_status, :subscription_status, :pricing_plan_id,
                  :credit_balance_credits, :created_at, :updated_at
                )
                """
            ),
            {
                "id": uuid4().hex,
                "owner_user_id": row["id"],
                "status": ACCOUNT_STATUS_INACTIVE,
                "billing_status": ACCOUNT_STATUS_INACTIVE,
                "subscription_status": ACCOUNT_STATUS_INACTIVE,
                "pricing_plan_id": "",
                "credit_balance_credits": 0,
                "created_at": now,
                "updated_at": now,
            },
        )


def ensure_account_for_user(user: Dict[str, Any]) -> Dict[str, Any]:
    init_billing_storage()
    with storage.engine().begin() as connection:
        row = connection.execute(
            text("SELECT * FROM accounts WHERE owner_user_id = :user_id"),
            {"user_id": user["id"]},
        ).mappings().first()
        if row:
            return dict(row)
        now = _iso_now()
        account_id = uuid4().hex
        connection.execute(
            text(
                """
                INSERT INTO accounts (
                  id, owner_user_id, status, billing_status, subscription_status, pricing_plan_id,
                  credit_balance_credits, created_at, updated_at
                ) VALUES (
                  :id, :owner_user_id, :status, :billing_status, :subscription_status, :pricing_plan_id,
                  :credit_balance_credits, :created_at, :updated_at
                )
                """
            ),
            {
                "id": account_id,
                "owner_user_id": user["id"],
                "status": ACCOUNT_STATUS_INACTIVE,
                "billing_status": ACCOUNT_STATUS_INACTIVE,
                "subscription_status": ACCOUNT_STATUS_INACTIVE,
                "pricing_plan_id": "",
                "credit_balance_credits": 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        return {
            "id": account_id,
            "owner_user_id": user["id"],
            "status": ACCOUNT_STATUS_INACTIVE,
            "billing_status": ACCOUNT_STATUS_INACTIVE,
            "subscription_status": ACCOUNT_STATUS_INACTIVE,
            "pricing_plan_id": "",
            "credit_balance_credits": 0,
            "created_at": now,
            "updated_at": now,
        }


def fetch_account_for_user(user_id: str) -> Optional[Dict[str, Any]]:
    if not billing_backend_ready():
        return None
    with storage.engine().begin() as connection:
        row = connection.execute(
            text("SELECT * FROM accounts WHERE owner_user_id = :user_id"),
            {"user_id": user_id},
        ).mappings().first()
    return dict(row) if row else None


def _fetch_pricing_plan_by_id(connection: Any, plan_id: str) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        text("SELECT * FROM pricing_plans WHERE id = :plan_id"),
        {"plan_id": plan_id},
    ).mappings().first()
    return dict(row) if row else None


def _fetch_pricing_plan_by_stripe_price(connection: Any, stripe_price_id: str) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        text("SELECT * FROM pricing_plans WHERE stripe_price_id = :stripe_price_id"),
        {"stripe_price_id": stripe_price_id},
    ).mappings().first()
    return dict(row) if row else None


def list_pricing_plans() -> List[Dict[str, Any]]:
    init_billing_storage()
    with storage.engine().begin() as connection:
        rows = connection.execute(
            text("SELECT * FROM pricing_plans WHERE active = TRUE ORDER BY display_order ASC, name ASC")
        ).mappings().all()
    return [dict(row) for row in rows]


def _model_access_from_plan(plan: Optional[Dict[str, Any]]) -> List[str]:
    if not plan:
        return []
    try:
        payload = json.loads(plan.get("model_access_json") or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in payload if str(item).strip()]


def provider_catalog_for_user(user: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    account = fetch_account_for_user(user["id"]) if user else None
    plan = None
    if account and account.get("pricing_plan_id"):
        with storage.engine().begin() as connection:
            plan = _fetch_pricing_plan_by_id(connection, str(account.get("pricing_plan_id", "")))
    allowed_models = set(_model_access_from_plan(plan))
    providers: List[Dict[str, Any]] = []
    for provider_id, label in orch.PROVIDER_LABELS.items():
        model_rows = []
        for model in orch.MODEL_SUGGESTIONS.get(provider_id, []):
            available, reason = platform_model_available(provider_id, model)
            model_rows.append(
                {
                    "id": model,
                    "available": available,
                    "reason": reason,
                    "allowed": (not allowed_models) or (_model_key(provider_id, model) in allowed_models),
                }
            )
        default_model = next(
            (item["id"] for item in model_rows if item["available"] and item["allowed"]),
            next((item["id"] for item in model_rows if item["available"]), model_rows[0]["id"] if model_rows else ""),
        )
        providers.append(
            {
                "id": provider_id,
                "label": label,
                "suggestedModels": [item["id"] for item in model_rows],
                "models": model_rows,
                "defaultModel": default_model,
                "defaultMaxOutputTokens": orch.default_tokens_for_provider(provider_id),
            }
        )
    return providers


def _estimate_tokens(text_value: str) -> int:
    if not text_value:
        return 1
    return max(1, math.ceil(len(text_value) / chars_per_token()))


def _synthetic_prior_turn(char_budget: int) -> orch.ConversationTurn:
    return orch.ConversationTurn(
        participant_id="synthetic",
        speaker_label="Synthetic",
        provider="openai",
        model="synthetic",
        round_number=1,
        turn_index=0,
        prompt="",
        response_text="X" * char_budget,
        usage={},
        raw_response={},
        is_summary=False,
    )


def _max_turn_prompt_tokens(question: str, participant: orch.ParticipantConfig, participants: List[orch.ParticipantConfig], summarizer: orch.ParticipantConfig) -> int:
    synthetic_turns = [_synthetic_prior_turn(orch.PROMPT_TRANSCRIPT_CHAR_BUDGET)]
    prompt = orch.compose_turn_prompt(participant, question, 1, participants, summarizer, synthetic_turns)
    return _estimate_tokens(prompt)


def _max_summary_prompt_tokens(question: str, summarizer: orch.ParticipantConfig, participants: List[orch.ParticipantConfig]) -> int:
    synthetic_turns = [_synthetic_prior_turn(50_000)]
    prompt = orch.compose_summary_prompt(question, participants, summarizer, synthetic_turns)
    return _estimate_tokens(prompt)


def _cost_for_tokens(pricing: Dict[str, Any], input_tokens: int, output_tokens: int) -> int:
    return math.ceil(
        (input_tokens * int(pricing["input_cost_per_1k_tokens_microusd"]))
        / 1000
    ) + math.ceil(
        (output_tokens * int(pricing["output_cost_per_1k_tokens_microusd"]))
        / 1000
    )


def credits_for_cost(max_cost_microusd: int) -> int:
    numerator = max_cost_microusd * margin_basis_points()
    denominator = 10_000 * credit_value_micro_usd()
    return max(1, math.ceil(numerator / denominator))


def _validate_model_access(account: Dict[str, Any], participants: List[orch.ParticipantConfig]) -> Optional[str]:
    if account.get("status") != ACCOUNT_STATUS_ACTIVE or account.get("subscription_status") not in ACTIVE_SUBSCRIPTION_STATUSES:
        return "An active paid subscription is required before you can run Pantheon."
    with storage.engine().begin() as connection:
        plan = _fetch_pricing_plan_by_id(connection, str(account.get("pricing_plan_id", "")))
    allowed = set(_model_access_from_plan(plan))
    if not allowed:
        return "Your account does not currently have model access configured."
    for participant in participants:
        key = _model_key(participant.provider, participant.model)
        if key not in allowed:
            return f"{participant.model} is not included in your current plan."
        available, reason = platform_model_available(participant.provider, participant.model)
        if not available:
            return reason
    return None


def estimate_run_cost(
    question: str,
    rounds: int,
    participants: List[orch.ParticipantConfig],
    summarizer_id: str,
    dry_run: bool,
    completed_turns: Optional[List[orch.ConversationTurn]] = None,
) -> Dict[str, Any]:
    if dry_run:
        if not dry_run_enabled():
            raise ValueError("Dry run is disabled in production billing mode.")
        return {
            "total_estimated_input_tokens": 0,
            "total_estimated_output_tokens": 0,
            "max_cost_microusd": 0,
            "required_credits": dry_run_credit_cost(),
            "per_call": [],
        }

    summarizer = orch.participant_by_id(participants, summarizer_id)
    completed_turns = list(completed_turns or [])
    step_type, _, next_participant = orch.determine_next_step(completed_turns, participants, rounds)
    pending: List[tuple[orch.ParticipantConfig, int, bool]] = []

    if step_type == "participant":
        remaining_conversation_turns = (rounds * len(participants)) - len([turn for turn in completed_turns if not turn.is_summary])
        participant_order = participants
        start_index = participant_order.index(next_participant) if next_participant else 0
        for offset in range(remaining_conversation_turns):
            participant = participant_order[(start_index + offset) % len(participant_order)]
            pending.append((participant, orch.PROMPT_TRANSCRIPT_CHAR_BUDGET, False))
        pending.append((summarizer, 50_000, True))
    elif step_type == "summary":
        pending.append((summarizer, 50_000, True))
    else:
        return {
            "total_estimated_input_tokens": 0,
            "total_estimated_output_tokens": 0,
            "max_cost_microusd": 0,
            "required_credits": 0,
            "per_call": [],
        }

    per_call: List[Dict[str, Any]] = []
    total_input = 0
    total_output = 0
    total_cost = 0
    for participant, _, is_summary in pending:
        pricing = configured_model_cost(participant.provider, participant.model)
        if pricing is None:
            raise ValueError(f"{participant.model} does not have a bounded cost profile configured.")
        if is_summary:
            input_tokens = _max_summary_prompt_tokens(question, summarizer, participants)
        else:
            input_tokens = _max_turn_prompt_tokens(question, participant, participants, summarizer)
        output_tokens = int(participant.max_output_tokens)
        call_cost = _cost_for_tokens(pricing, input_tokens, output_tokens)
        total_input += input_tokens
        total_output += output_tokens
        total_cost += call_cost
        per_call.append(
            {
                "participant_id": participant.participant_id,
                "label": participant.label,
                "provider": participant.provider,
                "model": participant.model,
                "estimated_input_tokens": input_tokens,
                "estimated_output_tokens": output_tokens,
                "max_cost_microusd": call_cost,
                "is_summary": is_summary,
            }
        )
    return {
        "total_estimated_input_tokens": total_input,
        "total_estimated_output_tokens": total_output,
        "max_cost_microusd": total_cost,
        "required_credits": credits_for_cost(total_cost),
        "per_call": per_call,
    }


def billing_snapshot_for_user(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    plans = list_pricing_plans() if billing_backend_ready() else []
    if not user:
        return {
            "ready": billing_backend_ready(),
            "stripeReady": stripe_ready(),
            "stripeCheckoutReady": stripe_checkout_ready(),
            "stripeWebhookReady": bool(stripe_webhook_secret()),
            "creditValueMicroUsd": credit_value_micro_usd(),
            "marginBasisPoints": margin_basis_points(),
            "plans": [_plan_to_api(item) for item in plans],
            "account": None,
            "messages": ["Log in and subscribe before running Pantheon."],
            "dryRunEnabled": dry_run_enabled(),
        }
    account = ensure_account_for_user(user)
    with storage.engine().begin() as connection:
        plan = _fetch_pricing_plan_by_id(connection, str(account.get("pricing_plan_id", ""))) if account.get("pricing_plan_id") else None
        customer = connection.execute(
            text("SELECT * FROM stripe_customers WHERE account_id = :account_id"),
            {"account_id": account["id"]},
        ).mappings().first()
        latest_subscription = connection.execute(
            text(
                """
                SELECT * FROM subscriptions
                WHERE account_id = :account_id
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"account_id": account["id"]},
        ).mappings().first()
    messages: List[str] = []
    if account.get("status") != ACCOUNT_STATUS_ACTIVE:
        messages.append("Choose a paid plan before starting a conversation.")
    elif int(account.get("credit_balance_credits", 0) or 0) <= 0:
        messages.append("Your credits are exhausted. Buy a credit pack before running another request.")
    return {
        "ready": billing_backend_ready(),
        "stripeReady": stripe_ready(),
        "stripeCheckoutReady": stripe_checkout_ready(),
        "stripeWebhookReady": bool(stripe_webhook_secret()),
        "creditValueMicroUsd": credit_value_micro_usd(),
        "marginBasisPoints": margin_basis_points(),
        "plans": [_plan_to_api(item) for item in plans],
        "account": {
            "id": account["id"],
            "status": account.get("status", ACCOUNT_STATUS_INACTIVE),
            "billingStatus": account.get("billing_status", ACCOUNT_STATUS_INACTIVE),
            "subscriptionStatus": account.get("subscription_status", ACCOUNT_STATUS_INACTIVE),
            "credits": int(account.get("credit_balance_credits", 0) or 0),
            "pricingPlanId": account.get("pricing_plan_id", ""),
            "pricingPlanName": plan.get("name", "") if plan else "",
            "pricingPlanType": plan.get("plan_type", "") if plan else "",
            "stripeCustomerId": customer.get("stripe_customer_id", "") if customer else "",
            "latestSubscriptionId": latest_subscription.get("stripe_subscription_id", "") if latest_subscription else "",
        },
        "messages": messages,
        "dryRunEnabled": dry_run_enabled(),
    }


def _plan_to_api(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": plan["id"],
        "name": plan.get("name", ""),
        "description": plan.get("description", ""),
        "planType": plan.get("plan_type", ""),
        "monthlyPriceCents": int(plan.get("monthly_price_cents", 0) or 0),
        "includedCredits": int(plan.get("included_credits", 0) or 0),
        "stripePriceConfigured": stripe_checkout_ready(),
        "modelAccess": _model_access_from_plan(plan),
    }


def quote_for_user(
    user: Optional[Dict[str, Any]],
    question: str,
    rounds: int,
    participants: List[orch.ParticipantConfig],
    summarizer_id: str,
    dry_run: bool,
    completed_turns: Optional[List[orch.ConversationTurn]] = None,
) -> Dict[str, Any]:
    if not billing_backend_ready():
        raise ValueError("Billing storage is not configured yet.")
    if not user:
        raise PermissionError("Log in and subscribe before running Pantheon.")
    account = ensure_account_for_user(user)
    error = _validate_model_access(account, participants)
    if error:
        raise ValueError(error)
    estimate = estimate_run_cost(question, rounds, participants, summarizer_id, dry_run, completed_turns)
    balance = int(account.get("credit_balance_credits", 0) or 0)
    sufficient = balance >= int(estimate["required_credits"])
    return {
        "requiredCredits": int(estimate["required_credits"]),
        "availableCredits": balance,
        "sufficientCredits": sufficient,
        "projectedBalance": balance - int(estimate["required_credits"]),
        "maxCostMicroUsd": int(estimate["max_cost_microusd"]),
        "estimatedInputTokens": int(estimate["total_estimated_input_tokens"]),
        "estimatedOutputTokens": int(estimate["total_estimated_output_tokens"]),
        "perCall": estimate["per_call"],
    }


def _locked_account(connection: Any, account_id: str) -> Dict[str, Any]:
    row = connection.execute(
        text("SELECT * FROM accounts WHERE id = :account_id FOR UPDATE"),
        {"account_id": account_id},
    ).mappings().first()
    if not row:
        raise ValueError("Billing account not found.")
    return dict(row)


def _append_ledger_entry(
    connection: Any,
    account: Dict[str, Any],
    *,
    amount_credits: int,
    entry_type: str,
    source_type: str,
    source_id: str,
    description: str,
    metadata: Optional[Dict[str, Any]] = None,
    stripe_event_id: str = "",
) -> Dict[str, Any]:
    current_balance = int(account.get("credit_balance_credits", 0) or 0)
    new_balance = current_balance + int(amount_credits)
    if new_balance < 0:
        raise ValueError("Insufficient credits.")
    created_at = _iso_now()
    row = connection.execute(
        text(
            """
            INSERT INTO credit_ledger (
              account_id, amount_credits, balance_after_credits, entry_type, source_type, source_id,
              stripe_event_id, description, metadata_json, created_at
            ) VALUES (
              :account_id, :amount_credits, :balance_after_credits, :entry_type, :source_type, :source_id,
              :stripe_event_id, :description, :metadata_json, :created_at
            )
            RETURNING id
            """
        ),
        {
            "account_id": account["id"],
            "amount_credits": int(amount_credits),
            "balance_after_credits": new_balance,
            "entry_type": entry_type,
            "source_type": source_type,
            "source_id": source_id,
            "stripe_event_id": stripe_event_id,
            "description": description,
            "metadata_json": _json_dump(metadata or {}),
            "created_at": created_at,
        },
    ).mappings().first()
    connection.execute(
        text(
            """
            UPDATE accounts
            SET credit_balance_credits = :credit_balance_credits,
                updated_at = :updated_at
            WHERE id = :account_id
            """
        ),
        {
            "credit_balance_credits": new_balance,
            "updated_at": created_at,
            "account_id": account["id"],
        },
    )
    account["credit_balance_credits"] = new_balance
    return {"id": row["id"], "balance_after_credits": new_balance, "created_at": created_at}


def reserve_credits_for_run(
    user: Dict[str, Any],
    *,
    conversation_id: str,
    question: str,
    rounds: int,
    participants: List[orch.ParticipantConfig],
    summarizer_id: str,
    dry_run: bool,
    completed_turns: Optional[List[orch.ConversationTurn]] = None,
) -> Dict[str, Any]:
    if not user or not user.get("id"):
        raise ValueError("Log in and subscribe before running Pantheon.")
    if not billing_backend_ready():
        raise ValueError("Billing storage is required before Pantheon can run.")
    account = ensure_account_for_user(user)
    validation_error = _validate_model_access(account, participants)
    if validation_error:
        raise ValueError(validation_error)
    estimate = estimate_run_cost(question, rounds, participants, summarizer_id, dry_run, completed_turns)
    required_credits = int(estimate["required_credits"])
    with storage.engine().begin() as connection:
        locked = _locked_account(connection, account["id"])
        if int(locked.get("credit_balance_credits", 0) or 0) < required_credits:
            raise ValueError("You do not have enough credits for this request.")
        usage_event_id = uuid4().hex
        _append_ledger_entry(
            connection,
            locked,
            amount_credits=-required_credits,
            entry_type="reservation",
            source_type="usage_reservation",
            source_id=usage_event_id,
            description=f"Reserved credits for {conversation_id}",
            metadata={
                "conversation_id": conversation_id,
                "question": question[:200],
            },
        )
        now = _iso_now()
        connection.execute(
            text(
                """
                INSERT INTO usage_events (
                  id, account_id, conversation_id, status, question_preview,
                  total_estimated_input_tokens, total_estimated_output_tokens,
                  max_cost_microusd, actual_cost_microusd, reserved_credits, actual_credits,
                  refunded_credits, participants_json, usage_json, created_at, updated_at, settled_at
                ) VALUES (
                  :id, :account_id, :conversation_id, :status, :question_preview,
                  :total_estimated_input_tokens, :total_estimated_output_tokens,
                  :max_cost_microusd, :actual_cost_microusd, :reserved_credits, :actual_credits,
                  :refunded_credits, :participants_json, :usage_json, :created_at, :updated_at, :settled_at
                )
                """
            ),
            {
                "id": usage_event_id,
                "account_id": account["id"],
                "conversation_id": conversation_id,
                "status": "reserved",
                "question_preview": question[:400],
                "total_estimated_input_tokens": int(estimate["total_estimated_input_tokens"]),
                "total_estimated_output_tokens": int(estimate["total_estimated_output_tokens"]),
                "max_cost_microusd": int(estimate["max_cost_microusd"]),
                "actual_cost_microusd": 0,
                "reserved_credits": required_credits,
                "actual_credits": 0,
                "refunded_credits": 0,
                "participants_json": json.dumps([orch.participant_to_metadata(item) for item in participants], ensure_ascii=True),
                "usage_json": "[]",
                "created_at": now,
                "updated_at": now,
                "settled_at": "",
            },
        )
        return {
            "usageEventId": usage_event_id,
            "reservedCredits": required_credits,
            "projectedBalance": int(locked.get("credit_balance_credits", 0) or 0),
            "quote": {
                "requiredCredits": required_credits,
                "availableCredits": int(account.get("credit_balance_credits", 0) or 0),
                "maxCostMicroUsd": int(estimate["max_cost_microusd"]),
            },
        }


def mark_usage_running(usage_event_id: str) -> None:
    with storage.engine().begin() as connection:
        connection.execute(
            text("UPDATE usage_events SET status = 'running', updated_at = :updated_at WHERE id = :id"),
            {"updated_at": _iso_now(), "id": usage_event_id},
        )


def settle_usage_for_run(
    usage_event_id: str,
    turns: List[orch.ConversationTurn],
    *,
    final_status: str,
) -> Dict[str, Any]:
    with storage.engine().begin() as connection:
        usage_event = connection.execute(
            text("SELECT * FROM usage_events WHERE id = :id FOR UPDATE"),
            {"id": usage_event_id},
        ).mappings().first()
        if not usage_event:
            raise ValueError("Usage reservation not found.")
        usage_row = dict(usage_event)
        account = _locked_account(connection, usage_row["account_id"])
        detailed_usage = []
        actual_cost_microusd = 0
        for turn in turns:
            metrics = orch.extract_usage_metrics(
                orch.ParticipantConfig(
                    participant_id=turn.participant_id,
                    label=turn.speaker_label,
                    provider=turn.provider,
                    model=turn.model,
                    max_output_tokens=max(int(turn.usage.get("output_tokens") or 0), 1),
                ),
                {"usage": turn.usage},
            )
            pricing = configured_model_cost(turn.provider, turn.model)
            if pricing is None or not any(metrics.values()):
                continue
            turn_cost = _cost_for_tokens(pricing, int(metrics["input_tokens"]), int(metrics["output_tokens"]))
            actual_cost_microusd += turn_cost
            detailed_usage.append(
                {
                    "participant_id": turn.participant_id,
                    "provider": turn.provider,
                    "model": turn.model,
                    "input_tokens": int(metrics["input_tokens"]),
                    "output_tokens": int(metrics["output_tokens"]),
                    "total_tokens": int(metrics["total_tokens"]),
                    "cost_microusd": turn_cost,
                    "is_summary": bool(turn.is_summary),
                }
            )

        reserved_credits = int(usage_row.get("reserved_credits", 0) or 0)
        if not turns:
            actual_credits = 0
        elif actual_cost_microusd <= 0:
            actual_credits = reserved_credits
        else:
            actual_credits = min(reserved_credits, credits_for_cost(actual_cost_microusd))
        refund_credits = max(reserved_credits - actual_credits, 0)

        if refund_credits:
            _append_ledger_entry(
                connection,
                account,
                amount_credits=refund_credits,
                entry_type="refund",
                source_type="usage_refund",
                source_id=usage_event_id,
                description=f"Refund unused credits for {usage_row.get('conversation_id', '')}",
                metadata={"usage_event_id": usage_event_id},
            )

        now = _iso_now()
        connection.execute(
            text(
                """
                UPDATE usage_events
                SET status = :status,
                    actual_cost_microusd = :actual_cost_microusd,
                    actual_credits = :actual_credits,
                    refunded_credits = :refunded_credits,
                    usage_json = :usage_json,
                    updated_at = :updated_at,
                    settled_at = :settled_at
                WHERE id = :id
                """
            ),
            {
                "status": final_status,
                "actual_cost_microusd": int(actual_cost_microusd),
                "actual_credits": actual_credits,
                "refunded_credits": refund_credits,
                "usage_json": json.dumps(detailed_usage, ensure_ascii=True),
                "updated_at": now,
                "settled_at": now,
                "id": usage_event_id,
            },
        )
        return {
            "reservedCredits": reserved_credits,
            "actualCredits": actual_credits,
            "refundedCredits": refund_credits,
            "actualCostMicroUsd": int(actual_cost_microusd),
            "balanceAfter": int(account.get("credit_balance_credits", 0) or 0),
        }


def _upsert_stripe_customer(connection: Any, *, account_id: str, stripe_customer_id: str, email: str) -> None:
    existing = connection.execute(
        text("SELECT account_id FROM stripe_customers WHERE account_id = :account_id"),
        {"account_id": account_id},
    ).mappings().first()
    params = {
        "account_id": account_id,
        "stripe_customer_id": stripe_customer_id,
        "email": email,
        "updated_at": _iso_now(),
    }
    if existing:
        connection.execute(
            text(
                """
                UPDATE stripe_customers
                SET stripe_customer_id = :stripe_customer_id,
                    email = :email,
                    updated_at = :updated_at
                WHERE account_id = :account_id
                """
            ),
            params,
        )
        return
    connection.execute(
        text(
            """
            INSERT INTO stripe_customers (account_id, stripe_customer_id, email, created_at, updated_at)
            VALUES (:account_id, :stripe_customer_id, :email, :created_at, :updated_at)
            """
        ),
        {
            **params,
            "created_at": params["updated_at"],
        },
    )


def _fetch_account_by_stripe_customer(connection: Any, stripe_customer_id: str) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        text(
            """
            SELECT accounts.*
            FROM stripe_customers
            JOIN accounts ON accounts.id = stripe_customers.account_id
            WHERE stripe_customers.stripe_customer_id = :stripe_customer_id
            """
        ),
        {"stripe_customer_id": stripe_customer_id},
    ).mappings().first()
    return dict(row) if row else None


def _fetch_account_by_id(connection: Any, account_id: str) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        text("SELECT * FROM accounts WHERE id = :account_id"),
        {"account_id": account_id},
    ).mappings().first()
    return dict(row) if row else None


def _recompute_account_state(connection: Any, account_id: str) -> None:
    subscription = connection.execute(
        text(
            """
            SELECT *
            FROM subscriptions
            WHERE account_id = :account_id
            ORDER BY
              CASE WHEN status = 'active' THEN 0 ELSE 1 END,
              updated_at DESC
            LIMIT 1
            """
        ),
        {"account_id": account_id},
    ).mappings().first()
    if subscription and str(subscription.get("status", "")) in ACTIVE_SUBSCRIPTION_STATUSES:
        connection.execute(
            text(
                """
                UPDATE accounts
                SET status = :status,
                    billing_status = :billing_status,
                    subscription_status = :subscription_status,
                    pricing_plan_id = :pricing_plan_id,
                    updated_at = :updated_at
                WHERE id = :account_id
                """
            ),
            {
                "status": ACCOUNT_STATUS_ACTIVE,
                "billing_status": str(subscription.get("status", "")),
                "subscription_status": str(subscription.get("status", "")),
                "pricing_plan_id": str(subscription.get("pricing_plan_id", "")),
                "updated_at": _iso_now(),
                "account_id": account_id,
            },
        )
        return
    latest_status = str(subscription.get("status", "")) if subscription else ACCOUNT_STATUS_INACTIVE
    connection.execute(
        text(
            """
            UPDATE accounts
            SET status = :status,
                billing_status = :billing_status,
                subscription_status = :subscription_status,
                pricing_plan_id = :pricing_plan_id,
                updated_at = :updated_at
            WHERE id = :account_id
            """
        ),
        {
            "status": ACCOUNT_STATUS_INACTIVE,
            "billing_status": latest_status,
            "subscription_status": latest_status,
            "pricing_plan_id": "",
            "updated_at": _iso_now(),
            "account_id": account_id,
        },
    )


def _ensure_remote_customer(user: Dict[str, Any], account: Dict[str, Any]) -> str:
    sdk = _stripe_client()
    with storage.engine().begin() as connection:
        row = connection.execute(
            text("SELECT * FROM stripe_customers WHERE account_id = :account_id"),
            {"account_id": account["id"]},
        ).mappings().first()
        if row:
            return str(row["stripe_customer_id"])

    customer = sdk.Customer.create(
        email=user.get("email", ""),
        name=user.get("name", "") or user.get("email", ""),
        metadata={"account_id": account["id"], "user_id": user["id"]},
    )
    with storage.engine().begin() as connection:
        _upsert_stripe_customer(
            connection,
            account_id=account["id"],
            stripe_customer_id=str(customer["id"]),
            email=str(user.get("email", "")),
        )
    return str(customer["id"])


def create_checkout_session(user: Dict[str, Any], plan_id: str, base_url: str) -> Dict[str, Any]:
    init_billing_storage()
    if not user or not user.get("id"):
        raise ValueError("Log in before opening Stripe Checkout.")
    account = ensure_account_for_user(user)
    with storage.engine().begin() as connection:
        plan = _fetch_pricing_plan_by_id(connection, plan_id)
    if not plan or not bool(plan.get("active", True)):
        raise ValueError("That pricing option is not available.")
    if not stripe_checkout_ready():
        raise RuntimeError("Stripe checkout is not configured on the server yet.")
    if not stripe_webhook_secret():
        raise RuntimeError("Stripe checkout is connected, but the webhook signing secret still needs to be configured before purchases can be activated.")

    customer_id = _ensure_remote_customer(user, account)
    sdk = _stripe_client()
    mode = "subscription" if plan.get("plan_type") == "subscription" else "payment"
    metadata = {
        "account_id": account["id"],
        "pricing_plan_id": plan["id"],
        "plan_type": plan.get("plan_type", ""),
        "user_id": user["id"],
    }
    session_payload = {
        "customer": customer_id,
        "mode": mode,
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": int(plan.get("monthly_price_cents", 0) or 0),
                    "product_data": {
                        "name": f"Pantheon {plan.get('name', '')}",
                        "description": str(plan.get("description", "") or ""),
                        "metadata": {
                            "plan_id": str(plan["id"]),
                            "plan_type": str(plan.get("plan_type", "")),
                        },
                    },
                    **(
                        {"recurring": {"interval": "month"}}
                        if plan.get("plan_type") == "subscription"
                        else {}
                    ),
                },
            }
        ],
        "success_url": f"{base_url}/pricing?success=checkout",
        "cancel_url": f"{base_url}/pricing?canceled=checkout",
        "metadata": metadata,
        "client_reference_id": account["id"],
        "allow_promotion_codes": False,
    }
    if mode == "subscription":
        session_payload["subscription_data"] = {"metadata": metadata}
    session = sdk.checkout.Session.create(
        **session_payload,
    )
    return {"url": str(session["url"])}


def create_billing_portal_session(user: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    account = ensure_account_for_user(user)
    with storage.engine().begin() as connection:
        customer = connection.execute(
            text("SELECT * FROM stripe_customers WHERE account_id = :account_id"),
            {"account_id": account["id"]},
        ).mappings().first()
    if not customer:
        raise ValueError("No Stripe customer exists for this account yet.")
    sdk = _stripe_client()
    portal = sdk.billing_portal.Session.create(
        customer=str(customer["stripe_customer_id"]),
        return_url=f"{base_url}/account",
    )
    return {"url": str(portal["url"])}


def _webhook_already_processed(connection: Any, event_id: str) -> bool:
    row = connection.execute(
        text("SELECT stripe_event_id FROM processed_webhook_events WHERE stripe_event_id = :event_id"),
        {"event_id": event_id},
    ).mappings().first()
    return bool(row)


def _mark_webhook_processed(connection: Any, event_id: str, event_type: str, payload: Dict[str, Any]) -> None:
    connection.execute(
        text(
            """
            INSERT INTO processed_webhook_events (stripe_event_id, event_type, processed_at, payload_json)
            VALUES (:stripe_event_id, :event_type, :processed_at, :payload_json)
            """
        ),
        {
            "stripe_event_id": event_id,
            "event_type": event_type,
            "processed_at": _iso_now(),
            "payload_json": json.dumps(payload, ensure_ascii=True),
        },
    )


def _grant_plan_credits_once(
    connection: Any,
    *,
    account_id: str,
    credits: int,
    source_type: str,
    source_id: str,
    description: str,
    stripe_event_id: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    existing = connection.execute(
        text(
            """
            SELECT id FROM credit_ledger
            WHERE source_type = :source_type AND source_id = :source_id
            LIMIT 1
            """
        ),
        {"source_type": source_type, "source_id": source_id},
    ).mappings().first()
    if existing:
        return
    account = _locked_account(connection, account_id)
    _append_ledger_entry(
        connection,
        account,
        amount_credits=int(credits),
        entry_type="credit",
        source_type=source_type,
        source_id=source_id,
        description=description,
        metadata=metadata or {},
        stripe_event_id=stripe_event_id,
    )


def handle_stripe_webhook(raw_body: bytes, signature: str) -> Dict[str, Any]:
    sdk = _stripe_client()
    try:
        event = sdk.Webhook.construct_event(raw_body, signature, stripe_webhook_secret())
    except Exception as exc:
        raise ValueError(f"Invalid Stripe webhook signature: {exc}") from exc

    payload = event.to_dict_recursive() if hasattr(event, "to_dict_recursive") else dict(event)
    event_id = str(payload.get("id", ""))
    event_type = str(payload.get("type", ""))
    obj = payload.get("data", {}).get("object", {})

    with storage.engine().begin() as connection:
        if _webhook_already_processed(connection, event_id):
            return {"ok": True, "duplicate": True}

        if event_type == "checkout.session.completed":
            account_id = str(obj.get("metadata", {}).get("account_id", "")).strip()
            stripe_customer_id = str(obj.get("customer", "")).strip()
            customer_email = str(obj.get("customer_details", {}).get("email", "") or obj.get("customer_email", "") or "")
            if account_id and stripe_customer_id:
                _upsert_stripe_customer(
                    connection,
                    account_id=account_id,
                    stripe_customer_id=stripe_customer_id,
                    email=customer_email,
                )
            if str(obj.get("mode", "")) == "payment" and account_id:
                plan = _fetch_pricing_plan_by_id(connection, str(obj.get("metadata", {}).get("pricing_plan_id", "")))
                if plan and plan.get("plan_type") == "credit_pack" and str(obj.get("payment_status", "")) == "paid":
                    _grant_plan_credits_once(
                        connection,
                        account_id=account_id,
                        credits=int(plan.get("included_credits", 0) or 0),
                        source_type="stripe_checkout_session",
                        source_id=str(obj.get("id", "")),
                        description=f"Credit pack: {plan.get('name', '')}",
                        stripe_event_id=event_id,
                        metadata={"pricing_plan_id": plan["id"]},
                    )

        elif event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
            stripe_customer_id = str(obj.get("customer", "")).strip()
            account = _fetch_account_by_stripe_customer(connection, stripe_customer_id)
            if not account:
                metadata_account_id = str(obj.get("metadata", {}).get("account_id", "")).strip()
                if metadata_account_id:
                    account = _fetch_account_by_id(connection, metadata_account_id)
                    if account and stripe_customer_id:
                        _upsert_stripe_customer(
                            connection,
                            account_id=account["id"],
                            stripe_customer_id=stripe_customer_id,
                            email="",
                        )
            if account:
                price_id = ""
                items = obj.get("items", {}).get("data", []) or []
                if items:
                    price_id = str(items[0].get("price", {}).get("id", "")).strip()
                metadata_plan_id = str(obj.get("metadata", {}).get("pricing_plan_id", "")).strip()
                plan = _fetch_pricing_plan_by_id(connection, metadata_plan_id) if metadata_plan_id else None
                if not plan and price_id:
                    plan = _fetch_pricing_plan_by_stripe_price(connection, price_id)
                existing = connection.execute(
                    text("SELECT id FROM subscriptions WHERE stripe_subscription_id = :stripe_subscription_id"),
                    {"stripe_subscription_id": str(obj.get("id", ""))},
                ).mappings().first()
                params = {
                    "account_id": account["id"],
                    "pricing_plan_id": plan["id"] if plan else "",
                    "stripe_customer_id": stripe_customer_id,
                    "stripe_subscription_id": str(obj.get("id", "")),
                    "stripe_price_id": price_id,
                    "status": str(obj.get("status", "")),
                    "current_period_start": str(obj.get("current_period_start", "") or ""),
                    "current_period_end": str(obj.get("current_period_end", "") or ""),
                    "cancel_at_period_end": bool(obj.get("cancel_at_period_end", False)),
                    "latest_invoice_id": str(obj.get("latest_invoice", "") or ""),
                    "updated_at": _iso_now(),
                }
                if existing:
                    connection.execute(
                        text(
                            """
                            UPDATE subscriptions
                            SET account_id = :account_id,
                                pricing_plan_id = :pricing_plan_id,
                                stripe_customer_id = :stripe_customer_id,
                                stripe_price_id = :stripe_price_id,
                                status = :status,
                                current_period_start = :current_period_start,
                                current_period_end = :current_period_end,
                                cancel_at_period_end = :cancel_at_period_end,
                                latest_invoice_id = :latest_invoice_id,
                                updated_at = :updated_at
                            WHERE stripe_subscription_id = :stripe_subscription_id
                            """
                        ),
                        params,
                    )
                else:
                    connection.execute(
                        text(
                            """
                            INSERT INTO subscriptions (
                              account_id, pricing_plan_id, stripe_customer_id, stripe_subscription_id, stripe_price_id,
                              status, current_period_start, current_period_end, cancel_at_period_end, latest_invoice_id,
                              created_at, updated_at
                            ) VALUES (
                              :account_id, :pricing_plan_id, :stripe_customer_id, :stripe_subscription_id, :stripe_price_id,
                              :status, :current_period_start, :current_period_end, :cancel_at_period_end, :latest_invoice_id,
                              :created_at, :updated_at
                            )
                            """
                        ),
                        {**params, "created_at": params["updated_at"]},
                    )
                _recompute_account_state(connection, account["id"])

        elif event_type == "invoice.paid":
            stripe_customer_id = str(obj.get("customer", "")).strip()
            account = _fetch_account_by_stripe_customer(connection, stripe_customer_id)
            if not account and str(obj.get("subscription", "")).strip():
                account = connection.execute(
                    text(
                        """
                        SELECT accounts.*
                        FROM subscriptions
                        JOIN accounts ON accounts.id = subscriptions.account_id
                        WHERE subscriptions.stripe_subscription_id = :stripe_subscription_id
                        LIMIT 1
                        """
                    ),
                    {"stripe_subscription_id": str(obj.get("subscription", "")).strip()},
                ).mappings().first()
                account = dict(account) if account else None
            if account:
                plan = None
                if str(obj.get("subscription", "")).strip():
                    subscription = connection.execute(
                        text(
                            """
                            SELECT * FROM subscriptions
                            WHERE stripe_subscription_id = :stripe_subscription_id
                            LIMIT 1
                            """
                        ),
                        {"stripe_subscription_id": str(obj.get("subscription", "")).strip()},
                    ).mappings().first()
                    if subscription and subscription.get("pricing_plan_id"):
                        plan = _fetch_pricing_plan_by_id(connection, str(subscription.get("pricing_plan_id", "")))
                if not plan:
                    line_items = obj.get("lines", {}).get("data", []) or []
                    for line in line_items:
                        price_id = str(line.get("price", {}).get("id", "")).strip()
                        plan = _fetch_pricing_plan_by_stripe_price(connection, price_id) if price_id else None
                        if plan and plan.get("plan_type") == "subscription":
                            break
                if plan and plan.get("plan_type") == "subscription":
                    _grant_plan_credits_once(
                        connection,
                        account_id=account["id"],
                        credits=int(plan.get("included_credits", 0) or 0),
                        source_type="stripe_invoice",
                        source_id=str(obj.get("id", "")),
                        description=f"Monthly credits: {plan.get('name', '')}",
                        stripe_event_id=event_id,
                        metadata={
                            "pricing_plan_id": plan["id"],
                            "stripe_subscription_id": str(obj.get("subscription", "")),
                        },
                    )

        _mark_webhook_processed(connection, event_id, event_type, payload)

    return {"ok": True, "type": event_type}
