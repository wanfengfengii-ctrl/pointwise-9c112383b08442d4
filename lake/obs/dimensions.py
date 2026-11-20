from datetime import date


def require_text(value: str, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value or '\x00' in value:
        raise ValueError(f'{field} must be nonempty text without surrounding whitespace')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError as error:
        raise ValueError(f'{field} must be valid UTF-8 text') from error
    return value


def validate_day(day: str) -> str:
    if type(day) is not str:
        raise ValueError('day must use YYYY-MM-DD')
    try:
        parsed = date.fromisoformat(day)
    except ValueError as error:
        raise ValueError('day must be a valid YYYY-MM-DD date') from error
    if parsed.isoformat() != day:
        raise ValueError('day must use YYYY-MM-DD')
    return day


def validate_district(district: str) -> str:
    return require_text(district, 'district')
