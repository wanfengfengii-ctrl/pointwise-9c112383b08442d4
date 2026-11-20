from lake.release.evidence import make_frozen_release
from lake.release.register import ReleaseRegister
from lake.release.selection import select_revision


def freeze(source, register: ReleaseRegister, issue_id, observation_id, revision):
    selected = select_revision(source, observation_id, revision)
    return register.save_frozen(make_frozen_release(issue_id, selected))
