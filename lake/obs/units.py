def to_millimeters(raw_value: int, unit: str) -> int:
    if type(raw_value) is not int:
        raise ValueError('raw_value must be an integer')
    if type(unit) is not str:
        raise ValueError('unit must be mm, cm or um')
    if unit == 'mm':
        return raw_value
    if unit == 'cm':
        return raw_value * 10
    if unit == 'um':
        millimeters, remainder = divmod(raw_value, 1000)
        if remainder:
            raise ValueError('raw_value cannot be represented in whole millimeters')
        return millimeters
    raise ValueError('unit must be mm, cm or um')


def format_integer(value: int) -> str:
    if type(value) is not int:
        raise ValueError('expected an integer')
    return format(value, 'x')


def parse_integer(value: str) -> int:
    if type(value) is not str:
        raise ValueError('expected canonical hexadecimal integer text')
    number = int(value, 16)
    if format_integer(number) != value:
        raise ValueError('expected canonical hexadecimal integer text')
    return number
