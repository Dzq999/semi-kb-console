"""add source-system scope to export_jobs

Revision ID: 0013_export_scope
Revises: 0012_export_filtered_flag
Create Date: 2026-09-08 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0013_export_scope'
down_revision = '0012_export_filtered_flag'
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    # 源系统一级维度导出子图：all/manufacturing/erp，默认 all（全部策展模块）。
    # 存在性守卫与 0011/0012 一致：schema 已由 create_all 建全时从头重跑迁移不得因列已存在而炸。
    if "scope" not in _columns("export_jobs"):
        op.add_column('export_jobs', sa.Column('scope', sa.String(length=20), nullable=False, server_default='all'))


def downgrade() -> None:
    if "scope" in _columns("export_jobs"):
        op.drop_column('export_jobs', 'scope')
