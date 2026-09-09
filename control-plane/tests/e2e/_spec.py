"""Ожидания по модели данных (docs/architectures/data-model.md), общие для тестов миграций."""

from __future__ import annotations

# Перечисления §4.2 модели данных: имя → значения в порядке объявления.
EXPECTED_ENUMS: dict[str, list[str]] = {
    "user_status": ["active", "blocked", "deleted"],
    "admin_role": ["super_admin", "admin", "operator", "support"],
    "admin_status": ["active", "blocked"],
    "email_token_kind": ["verify", "reset"],
    "auth_event_kind": ["register", "login", "logout", "reset"],
    "plan_status": ["active", "archived"],
    "inbound_profile": ["vless_raw_vision", "vless_xhttp", "trojan_reality"],
    "node_status": [
        "pending",
        "provisioning",
        "active",
        "degraded",
        "offline",
        "maintenance",
        "disabled",
        "suspended",
    ],
    "user_node_state": ["active", "suspended_quota", "suspended_admin", "expired", "removed"],
    "command_type": ["restart_xray", "rotate_credentials", "collect_diagnostics", "update_agent"],
    "command_status": ["issued", "delivered", "applied", "failed", "expired"],
    "subscription_state": ["none", "active", "suspended_quota", "suspended_admin", "expired"],
    "period_source": ["redeem", "admin", "order"],
    "balance_source": ["report", "adjustment", "bonus", "late_report"],
    "code_kind": ["redeem", "promo"],
    "report_status": ["accepted", "duplicate", "rejected_time", "held_anomaly"],
    "reconciliation_kind": ["arithmetic", "cross_source", "continuity"],
    "event_type": [
        "subscription_activated",
        "subscription_expiring",
        "subscription_expired",
        "traffic_80",
        "traffic_95",
        "traffic_exhausted",
        "node_address_changed",
        "node_offline",
        "node_recovered",
        "node_suspended_by_provider",
        "reconciliation_mismatch",
        "node_report_buffer_full",
    ],
    "delivery_status": ["pending", "sent", "bounced", "failed"],
    "job_queue": ["critical", "background"],
    "job_status": ["pending", "running", "done", "failed", "dead"],
    "actor_type": ["admin", "user", "system"],
}

# Таблицы групп схемы (по номерам миграций плана).
IDENTITY_TABLES = {"users", "admin_users", "admin_recovery_codes", "email_tokens", "auth_events"}
CATALOG_TABLES = {
    "plans",
    "plan_protocols",
    "access_groups",
    "plan_access_groups",
    "billing_groups",
    "billing_group_multipliers",
    "node_billing_assignments",
}
NODES_TABLES = {
    "nodes",
    "node_ip_history",
    "node_access_groups",
    "bootstrap_tokens",
    "node_identities",
    "inbounds",
    "inbound_secrets",
    "node_config_versions",
    "node_user_credentials",
    "node_user_state",
    "commands",
    "node_metrics",
    "node_country_availability",
}
SUBSCRIPTIONS_TABLES = {
    "subscription_periods",
    "subscriptions",
    "balance_entries",
    "subscription_tokens",
    "subscription_access_log",
    "codes",
    "code_redemptions",
    "orders",
    "payments",
}
ACCOUNTING_TABLES = {
    "traffic_reports",
    "traffic_lines",
    "traffic_hourly",
    "traffic_daily",
    "node_interface_hourly",
    "traffic_gaps",
    "reconciliation_runs",
    "quota_grants",
    "user_online_ips",
    "user_blocked_ips",
    "partition_policies",
}
OPS_TABLES = {
    "events",
    "email_deliveries",
    "webhook_deliveries",
    "jobs",
    "audit_log",
    "settings",
}
