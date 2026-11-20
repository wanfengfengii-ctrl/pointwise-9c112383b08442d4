from lake.obs.revisions import ObservationRevision, validate_identity, validate_revision
from lake.obs.units import format_integer, parse_integer
from lake.sqlite import open_database, transaction


class RevisionConflict(ValueError):
    pass


class RevisionStore:
    def __init__(self, path):
        self._db = open_database(path)
        try:
            self._db.execute('''
                CREATE TABLE IF NOT EXISTS revisions (
                    observation_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    day TEXT NOT NULL,
                    district TEXT NOT NULL,
                    raw_value TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    millimeters TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    PRIMARY KEY (observation_id, revision)
                )
            ''')
        except BaseException:
            self.close()
            raise

    def close(self):
        self._db.close()

    def put_revision(self, value: ObservationRevision) -> ObservationRevision:
        validate_revision(value)
        with transaction(self._db, write=True):
            self._db.execute('''
                INSERT INTO revisions
                    (observation_id, revision, day, district, raw_value, unit, millimeters, provenance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (observation_id, revision) DO NOTHING
            ''', (
                value.observation_id, format_integer(value.revision), value.day, value.district,
                format_integer(value.raw_value), value.unit, format_integer(value.millimeters),
                value.provenance,
            ))
            saved = self.get_revision(value.observation_id, value.revision)
            if saved != value:
                raise RevisionConflict('revision identity already has different content')
            return saved

    def get_revision(self, observation_id, revision) -> ObservationRevision | None:
        validate_identity(observation_id, revision)
        row = self._db.execute('''
            SELECT * FROM revisions WHERE observation_id = ? AND revision = ?
        ''', (observation_id, format_integer(revision))).fetchone()
        if row is None:
            return None
        return ObservationRevision(
            row['observation_id'], parse_integer(row['revision']), row['day'], row['district'],
            parse_integer(row['raw_value']), row['unit'], parse_integer(row['millimeters']),
            row['provenance'],
        )
