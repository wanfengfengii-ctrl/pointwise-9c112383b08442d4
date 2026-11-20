from lake.obs.revisions import make_revision


def record(source, observation_id, revision, day, district, raw_value, unit, provenance):
    """Validate and register a named reading, returning the stored revision."""
    value = make_revision(observation_id, revision, day, district, raw_value, unit, provenance)
    return source.put_revision(value)
