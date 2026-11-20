import json
from dataclasses import asdict, fields

from lake.obs.dimensions import require_text
from lake.obs.revisions import ObservationRevision
from lake.obs.units import format_integer, parse_integer
from lake.release.evidence import FrozenRelease

_INTEGER_FIELDS = ('revision', 'raw_value', 'millimeters')
_TARGET_FIELDS = frozenset(field.name for field in fields(ObservationRevision))


def encode_release(frozen: FrozenRelease) -> str:
    target = asdict(frozen.target)
    for name in _INTEGER_FIELDS:
        target[name] = format_integer(target[name])
    return json.dumps(
        {'version': 1, 'issue_id': frozen.issue_id, 'target': target},
        sort_keys=True, separators=(',', ':'), ensure_ascii=True,
    )


def _unique_members(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f'duplicate release field {name}')
        result[name] = value
    return result


def _read_header(wire):
    if type(wire) is not str:
        raise ValueError('release must be JSON text')
    try:
        envelope = json.loads(wire, object_pairs_hook=_unique_members)
    except (ValueError, RecursionError) as error:
        raise ValueError('invalid release JSON') from error
    if type(envelope) is not dict:
        raise ValueError('release must be an object')
    version = envelope.get('version')
    if type(version) is not int or version != 1:
        raise ValueError('unknown release version')
    require_text(envelope.get('issue_id'), 'issue_id')
    return envelope


def release_issue_id(wire: str) -> str:
    return _read_header(wire)['issue_id']


def decode_release(wire: str) -> FrozenRelease:
    envelope = _read_header(wire)
    if envelope.keys() != {'version', 'issue_id', 'target'}:
        raise ValueError('invalid release fields')
    target = envelope['target']
    if type(target) is not dict or target.keys() != _TARGET_FIELDS:
        raise ValueError('incomplete target evidence')
    if any(type(value) is not str for value in target.values()):
        raise ValueError('invalid target field type')
    for name in _INTEGER_FIELDS:
        target[name] = parse_integer(target[name])
    return FrozenRelease(envelope['issue_id'], ObservationRevision(**target))
