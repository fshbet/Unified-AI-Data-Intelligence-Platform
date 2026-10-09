"""anonymisation layer: per-column policy + token vault

Revision ID: 97842dad8a64
Revises: 13140b4c3793
Create Date: 2026-10-08 21:26:17.043552

Two things this migration has to survive, both of which broke the first version of it:

1. A **populated catalog.** `columns.anon_params` is NOT NULL, so it needs a server_default or
   the ALTER fails on every existing row.
2. **`create_all` having run first.** `backend/main.py` calls `Base.metadata.create_all()` on
   startup, so if the new code is started before `alembic upgrade` — the normal order of events
   on a real deployment — `anon_jobs` and `anon_tokens` already exist. A plain `create_table`
   then aborts the whole migration, including the ALTER that `create_all` can never perform,
   leaving the database permanently stuck. So the creates are conditional.
"""
from alembic import op
import sqlalchemy as sa

revision = '97842dad8a64'
down_revision = '13140b4c3793'
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    existing = _tables()

    if 'anon_jobs' not in existing:
        op.create_table(
            'anon_jobs',
            sa.Column('id', sa.String(length=32), nullable=False),
            sa.Column('conversation_id', sa.String(length=32), nullable=True),
            sa.Column('user_id', sa.String(length=32), nullable=True),
            sa.Column('salt', sa.String(length=64), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('purged_at', sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )
        with op.batch_alter_table('anon_jobs', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_anon_jobs_conversation_id'), ['conversation_id'], unique=False)
            batch_op.create_index(batch_op.f('ix_anon_jobs_created_at'), ['created_at'], unique=False)

    if 'anon_tokens' not in existing:
        op.create_table(
            'anon_tokens',
            sa.Column('job_id', sa.String(length=32), nullable=False),
            sa.Column('token', sa.String(length=32), nullable=False),
            sa.Column('entity', sa.String(length=16), nullable=False),
            sa.Column('sequence', sa.Integer(), nullable=False),
            sa.Column('fingerprint', sa.String(length=64), nullable=False),
            sa.Column('original_enc', sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(['job_id'], ['anon_jobs.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('job_id', 'token'),
        )
        with op.batch_alter_table('anon_tokens', schema=None) as batch_op:
            batch_op.create_index('anon_tokens_fp', ['job_id', 'fingerprint'], unique=True)

    have = _columns('columns')
    with op.batch_alter_table('columns', schema=None) as batch_op:
        if 'anon_strategy' not in have:
            batch_op.add_column(sa.Column('anon_strategy', sa.String(length=16), nullable=True))
        if 'anon_entity' not in have:
            batch_op.add_column(sa.Column('anon_entity', sa.String(length=16), nullable=True))
        if 'anon_params' not in have:
            # server_default so this applies to an already-populated catalog; every existing
            # column simply starts with empty policy params.
            batch_op.add_column(sa.Column('anon_params', sa.JSON(), nullable=False, server_default='{}'))
        if 'anon_accepted_by' not in have:
            batch_op.add_column(sa.Column('anon_accepted_by', sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('columns', schema=None) as batch_op:
        batch_op.drop_column('anon_accepted_by')
        batch_op.drop_column('anon_params')
        batch_op.drop_column('anon_entity')
        batch_op.drop_column('anon_strategy')

    with op.batch_alter_table('anon_tokens', schema=None) as batch_op:
        batch_op.drop_index('anon_tokens_fp')
    op.drop_table('anon_tokens')

    with op.batch_alter_table('anon_jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_anon_jobs_created_at'))
        batch_op.drop_index(batch_op.f('ix_anon_jobs_conversation_id'))
    op.drop_table('anon_jobs')
