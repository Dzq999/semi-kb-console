"""add editable fixed-text template to report_settings

Revision ID: 0014_report_template
Revises: 0013_export_scope
Create Date: 2026-09-08 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0014_report_template'
down_revision = '0013_export_scope'
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    # 日报固定版式文案的用户自定义模版（title_prefix/note_tpl/criteria_tpl 的 JSON）；NULL=用内置默认。
    # 存在性守卫与 0011/0012/0013 一致：schema 已由 create_all 建全时从头重跑迁移不得因列已存在而炸。
    if "report_template_json" not in _columns("report_settings"):
        op.add_column('report_settings', sa.Column('report_template_json', sa.Text(), nullable=True))


def downgrade() -> None:
    if "report_template_json" in _columns("report_settings"):
        op.drop_column('report_settings', 'report_template_json')
