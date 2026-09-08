"""add filtered flag to export_jobs

Revision ID: 0012_export_filtered_flag
Revises: 0011_report_email_toggle
Create Date: 2025-01-15 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0012_export_filtered_flag'
down_revision = '0011_report_email_toggle'
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    # 添加 filtered 字段，默认为 True（精简导出）。
    # 存在性守卫与本目录其它迁移一致：schema 已由 create_all 建全（如测试 fixture）时，
    # 从头重跑迁移不得因列已存在而炸——SQLite ADD COLUMN 无 IF NOT EXISTS，必须先查。
    if "filtered" not in _columns("export_jobs"):
        op.add_column('export_jobs', sa.Column('filtered', sa.Boolean(), nullable=False, server_default='1'))


def downgrade() -> None:
    if "filtered" in _columns("export_jobs"):
        op.drop_column('export_jobs', 'filtered')
