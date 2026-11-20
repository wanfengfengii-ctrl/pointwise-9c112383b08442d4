from lake.obs.store import RevisionStore


class RevisionNotFound(LookupError):
    pass


def select_revision(source: RevisionStore, observation_id: str, revision: int):
    result = source.get_revision(observation_id, revision)
    if result is None:
        raise RevisionNotFound('the requested observation revision is absent')
    return result
