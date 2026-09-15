"""Track C: ontology governance jobs, synonym-candidate lifecycle, and cron schedule."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_ontology_governance"
down_revision = "0015_llm_stream_mode"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _tables()
    if "governance_jobs" not in existing:
        op.create_table(
            "governance_jobs",
            sa.Column("id", sa.String(length=48), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="scan"),
            sa.Column("trigger", sa.String(length=16), nullable=False, server_default="manual"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
            sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
            sa.Column("stage", sa.String(length=40), nullable=True),
            sa.Column("candidates_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("worker_id", sa.String(length=120), nullable=True),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("audit_path", sa.Text(), nullable=True),
            sa.Column("output", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "governance_candidates" not in existing:
        op.create_table(
            "governance_candidates",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("cluster_key", sa.String(length=300), nullable=False, index=True),
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="property"),
            sa.Column("canonical", sa.String(length=200), nullable=False),
            sa.Column("canonical_module", sa.String(length=120), nullable=True),
            sa.Column("merge_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("reason_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("label_zh", sa.String(length=200), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="detected", index=True),
            sa.Column("detected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("cluster_key", name="uq_governance_candidate_cluster"),
        )
    if "governance_schedules" not in existing:
        op.create_table(
            "governance_schedules",
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("cron", sa.String(length=120), nullable=False, server_default="0 3 * * *"),
            sa.Column("timezone", sa.String(length=80), nullable=False, server_default="Asia/Shanghai"),
            sa.Column("auto_deprecate", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("grace_days", sa.Integer(), nullable=False, server_default="14"),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    existing = _tables()
    for table in ("governance_schedules", "governance_candidates", "governance_jobs"):
        if table in existing:
            op.drop_table(table)
