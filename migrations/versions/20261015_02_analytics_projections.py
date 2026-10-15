"""Versioned record generations and catalog invalidation."""
from alembic import op

revision = '20261015_02'
down_revision = '20261015_01'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE analytics_states (
	app_user_id BIGINT NOT NULL,
	rules_version VARCHAR(32) NOT NULL,
	source_revision BIGINT DEFAULT '0' NOT NULL,
	active_generation UUID,
	active_revision BIGINT,
	building_generation UUID,
	building_revision BIGINT,
	status VARCHAR(16) DEFAULT 'pending' NOT NULL,
	cursor JSONB,
	last_error TEXT,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (app_user_id, rules_version),
	CONSTRAINT ck_analytics_states_status CHECK (status IN ('pending','rebuilding','ready','disabled')),
	CONSTRAINT ck_analytics_states_revisions CHECK (source_revision >= 0 AND (active_revision IS NULL OR active_revision BETWEEN 0 AND source_revision) AND (building_revision IS NULL OR building_revision BETWEEN 0 AND source_revision)),
	CONSTRAINT ck_analytics_states_generations CHECK ((active_generation IS NULL) = (active_revision IS NULL) AND (building_generation IS NULL) = (building_revision IS NULL)),
	CONSTRAINT ck_analytics_states_ready CHECK (status <> 'ready' OR (active_generation IS NOT NULL AND active_revision = source_revision)),
	FOREIGN KEY(app_user_id) REFERENCES app_users (id) ON DELETE CASCADE
)
""")
    op.execute('CREATE INDEX ix_analytics_states_status ON analytics_states (status, updated_at)')
    op.execute("""
CREATE TABLE analytics_record_projections (
	id BIGSERIAL NOT NULL,
	app_user_id BIGINT NOT NULL,
	exercise_id INTEGER NOT NULL,
	gym_scope VARCHAR(64) NOT NULL,
	rules_version VARCHAR(32) NOT NULL,
	generation UUID NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_analytics_record_projection UNIQUE (app_user_id, exercise_id, gym_scope, rules_version, generation),
	CONSTRAINT ck_analytics_projection_scope CHECK (gym_scope IN ('all','unknown') OR gym_scope ~ '^gym:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'),
	FOREIGN KEY(app_user_id) REFERENCES app_users (id) ON DELETE CASCADE,
	FOREIGN KEY(exercise_id) REFERENCES exercises (id) ON DELETE CASCADE
)
""")
    op.execute('CREATE INDEX ix_analytics_projection_exercise ON analytics_record_projections (exercise_id)')
    op.execute('CREATE INDEX ix_analytics_projection_generation ON analytics_record_projections (app_user_id, rules_version, generation)')
    op.execute("""
CREATE FUNCTION invalidate_analytics_muscle_mapping() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.main_muscle_group IS DISTINCT FROM NEW.main_muscle_group
       OR OLD.secondary_muscle_groups IS DISTINCT FROM NEW.secondary_muscle_groups THEN
        INSERT INTO analytics_states (app_user_id, rules_version, source_revision, status)
        SELECT DISTINCT ws.app_user_id, 'p1-08-v1', 1, 'pending'
        FROM workout_session_exercises se
        JOIN workout_sessions ws ON ws.id = se.workout_session_id
        WHERE se.exercise_id = NEW.id AND ws.status = 'finished'
        ORDER BY ws.app_user_id
        ON CONFLICT (app_user_id, rules_version) DO UPDATE
        SET source_revision = analytics_states.source_revision + 1,
            status = 'pending', updated_at = now();
    END IF;
    RETURN NEW;
END;
$$
""")
    op.execute("CREATE TRIGGER invalidate_analytics_muscle_mapping AFTER UPDATE OF main_muscle_group, secondary_muscle_groups ON exercises FOR EACH ROW EXECUTE FUNCTION invalidate_analytics_muscle_mapping()")


def downgrade():
    op.execute('DROP TRIGGER invalidate_analytics_muscle_mapping ON exercises')
    op.execute('DROP FUNCTION invalidate_analytics_muscle_mapping()')
    op.drop_table('analytics_record_projections')
    op.drop_table('analytics_states')
