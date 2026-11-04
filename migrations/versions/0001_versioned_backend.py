"""Additive adoption of the original development schema; no data is dropped.

The schema snapshot below is frozen. Future model changes require new migrations;
do not edit this released baseline in place.
"""
from alembic import op
import sqlalchemy as sa

revision = '0001_versioned_backend'
down_revision = None
branch_labels = None
depends_on = None


# Frozen baseline schema: later migrations do not change this metadata.
metadata = sa.MetaData()
sa.Table('chat_events', metadata,
    sa.Column('event_id', sa.String(length=36), nullable=False, primary_key=True),
    sa.Column('chat_id', sa.String(length=100), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(length=40), nullable=False),
    sa.Column('bot_id', sa.String(length=30), nullable=False),
    sa.Column('task_id', sa.String(length=36), nullable=True),
    sa.Column('version', sa.Integer(), nullable=True),
    sa.Column('message_id', sa.String(length=36), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('dedupe_key', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint('chat_id', 'sequence'),
    sa.UniqueConstraint('dedupe_key'),
)
sa.Index('ix_chat_events_chat_id', metadata.tables['chat_events'].c['chat_id'], unique=False)

sa.Table('chat_states', metadata,
    sa.Column('chat_id', sa.String(length=100), nullable=False, primary_key=True),
    sa.Column('last_sequence', sa.Integer(), nullable=False),
    sa.Column('last_message_sequence', sa.Integer(), nullable=False),
)

sa.Table('tasks', metadata,
    sa.Column('task_id', sa.String(length=36), nullable=False, primary_key=True),
    sa.Column('message_id', sa.String(length=36), nullable=False),
    sa.Column('chat_id', sa.String(length=100), nullable=False),
    sa.Column('user_id', sa.String(length=100), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('result', sa.Text(), nullable=True),
)
sa.Index('ix_tasks_chat_id', metadata.tables['tasks'].c['chat_id'], unique=False)

sa.Table('messages', metadata,
    sa.Column('message_id', sa.String(length=36), nullable=False, primary_key=True),
    sa.Column('chat_id', sa.String(length=100), nullable=False),
    sa.Column('user_id', sa.String(length=100), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('task_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('routing_status', sa.String(length=20), nullable=False, server_default='stored_no_action'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.task_id']),
)
sa.Index('ix_messages_chat_id', metadata.tables['messages'].c['chat_id'], unique=False)
sa.Index('ix_messages_routing_status', metadata.tables['messages'].c['routing_status'], unique=False)

sa.Table('task_versions', metadata,
    sa.Column('task_id', sa.String(length=36), nullable=False, primary_key=True),
    sa.Column('version', sa.Integer(), nullable=False, primary_key=True),
    sa.Column('message_id', sa.String(length=36), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('result', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('run_token', sa.String(length=36), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.task_id']),
)
sa.Index('ix_task_versions_status', metadata.tables['task_versions'].c['status'], unique=False)

sa.Table('dependency_decisions', metadata,
    sa.Column('decision_id', sa.String(length=36), nullable=False, primary_key=True),
    sa.Column('task_id', sa.String(length=36), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('prerequisite_task_id', sa.String(length=36), nullable=False),
    sa.Column('original_version', sa.Integer(), nullable=False),
    sa.Column('updated_version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('choice', sa.String(length=20), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['prerequisite_task_id', 'original_version'], ['task_versions.task_id', 'task_versions.version']),
    sa.ForeignKeyConstraint(['prerequisite_task_id', 'updated_version'], ['task_versions.task_id', 'task_versions.version']),
    sa.ForeignKeyConstraint(['task_id', 'version'], ['task_versions.task_id', 'task_versions.version']),
)
sa.Index('ix_dependency_decisions_task_id', metadata.tables['dependency_decisions'].c['task_id'], unique=False)

sa.Table('task_dependencies', metadata,
    sa.Column('task_id', sa.String(length=36), nullable=False, primary_key=True),
    sa.Column('version', sa.Integer(), nullable=False, primary_key=True),
    sa.Column('prerequisite_task_id', sa.String(length=36), nullable=False, primary_key=True),
    sa.Column('prerequisite_version', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['prerequisite_task_id', 'prerequisite_version'], ['task_versions.task_id', 'task_versions.version']),
    sa.ForeignKeyConstraint(['task_id', 'version'], ['task_versions.task_id', 'task_versions.version']),
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    additions = {
        'task_versions': [sa.Column('message_id', sa.String(36)), sa.Column('error', sa.Text()),
                          sa.Column('run_token', sa.String(36)),
                          sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
                          sa.Column('completed_at', sa.DateTime(timezone=True))],
        'messages': [sa.Column('sequence', sa.Integer(), nullable=False, server_default='0'),
                     sa.Column('routing_status', sa.String(20), nullable=False, server_default='stored_no_action')],
    }
    for table, columns in additions.items():
        if table in existing:
            names = {c['name'] for c in inspector.get_columns(table)}
            for column in columns:
                if column.name not in names:
                    op.add_column(table, column)
    metadata.create_all(bind)
    for table in metadata.sorted_tables:
        for index in table.indexes:
            index.create(bind, checkfirst=True)
    # Original workers wrote tasks, so its current snapshot is authoritative at adoption.
    bind.execute(sa.text('''INSERT INTO task_versions
        (task_id, version, message_id, content, status, result, created_at, attempts)
        SELECT t.task_id, t.version, t.message_id, t.content, t.status, t.result, CURRENT_TIMESTAMP, 0
        FROM tasks t WHERE NOT EXISTS
        (SELECT 1 FROM task_versions v WHERE v.task_id=t.task_id AND v.version=t.version)'''))
    for row in bind.execute(sa.text('SELECT task_id, version, message_id, content, status, result FROM tasks')).mappings().all():
        bind.execute(sa.text('''UPDATE task_versions SET message_id=:message_id, content=:content,
            status=:status, result=:result WHERE task_id=:task_id AND version=:version'''), dict(row))
    bind.execute(sa.text("UPDATE task_versions SET completed_at=CURRENT_TIMESTAMP WHERE status IN ('completed','failed','cancelled') AND completed_at IS NULL"))
    chats = bind.execute(sa.text('SELECT chat_id FROM messages UNION SELECT chat_id FROM tasks')).scalars().all()
    for chat_id in chats:
        messages = bind.execute(sa.text('SELECT message_id FROM messages WHERE chat_id=:chat ORDER BY created_at, message_id'), {'chat': chat_id}).scalars().all()
        for sequence, message_id in enumerate(messages, 1):
            bind.execute(sa.text('UPDATE messages SET sequence=:seq WHERE message_id=:id'), {'seq': sequence, 'id': message_id})
        bind.execute(sa.text('''INSERT INTO chat_states (chat_id, last_sequence, last_message_sequence)
            VALUES (:chat, 0, :seq)'''), {'chat': chat_id, 'seq': len(messages)})


def downgrade():
    raise RuntimeError('Destructive downgrade is intentionally disabled. Restore a backup to roll back.')
