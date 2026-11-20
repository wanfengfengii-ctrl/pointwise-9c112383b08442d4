from dataclasses import dataclass

from lake.obs.dimensions import require_text, validate_day, validate_district
from lake.obs.units import to_millimeters


@dataclass(frozen=True)
class ObservationRevision:
    observation_id: str
    revision: int
    day: str
    district: str
    raw_value: int
    unit: str
    millimeters: int
    provenance: str


def validate_identity(observation_id: str, revision: int) -> None:
    require_text(observation_id, 'observation_id')
    if type(revision) is not int or revision <= 0:
        raise ValueError('revision must be a positive integer')


def make_revision(observation_id, revision, day, district, raw_value, unit, provenance):
    validate_identity(observation_id, revision)
    return ObservationRevision(
        observation_id, revision, validate_day(day), validate_district(district),
        raw_value, unit, to_millimeters(raw_value, unit), require_text(provenance, 'provenance'),
    )


def validate_revision(value: ObservationRevision) -> None:
    if type(value) is not ObservationRevision:
        raise ValueError('expected an observation revision')
    expected = make_revision(
        value.observation_id, value.revision, value.day, value.district,
        value.raw_value, value.unit, value.provenance,
    )
    if type(value.millimeters) is not int or value.millimeters != expected.millimeters:
        raise ValueError('target millimeters do not agree with the raw reading')
