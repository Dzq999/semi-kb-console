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


def upgrade() -> None:
    # 添加 filtered 字段，默认为 True（精简导出）
    op.add_column('export_jobs', sa.Column('filtered', sa.Boolean(), nullable=False, server_default='1'))


def downgrade() -> None:
    op.drop_column('export_jobs', 'filtered')
