"""Mass-fraction bounds from printed decimal resolution, not paper-specific tolerances.

These are rounding intervals, not measurement uncertainties. The caller must
establish that the selected cells are the complete components of one mass basis.
"""
from decimal import Decimal, InvalidOperation, localcontext
import math
import re


def printed_interval(cell):
    if not isinstance(cell, str):
        raise ValueError('printed mass requires original cell text')
    token = cell.strip().replace('\u2212', '-')
    if not re.fullmatch(r'\+?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', token):
        raise ValueError('mass cell is missing, qualified, negative, or unsupported')
    try:
        value = Decimal(token)
        if not value.is_finite() or value.adjusted() > 100 or value.as_tuple().exponent < -100:
            raise ValueError('mass precision outside supported range')
        half_step = Decimal(10) ** value.as_tuple().exponent / 2
        return value, max(Decimal(0), value - half_step), value + half_step
    except InvalidOperation as error:
        raise ValueError('invalid printed mass') from error


def composition_bounds(cells):
    """Return nominal percentages and outward ratio bounds for any component count."""
    if not cells:
        raise ValueError('empty mass basis')
    with localcontext() as context:
        context.prec = 220
        intervals = [printed_interval(cell) for cell in cells]
        total = sum(value for value, _, _ in intervals)
        if total <= 0:
            raise ValueError('mass basis total must be positive')
        percentages, bounds = [], []
        for index, (value, low, high) in enumerate(intervals):
            other_low = sum(lo for j, (_, lo, _) in enumerate(intervals) if j != index)
            other_high = sum(hi for j, (_, _, hi) in enumerate(intervals) if j != index)
            lower = 100 * low / (low + other_high) if low + other_high else Decimal(100)
            upper = 100 * high / (high + other_low)
            percentages.append(float(100 * value / total))
            lower_float, upper_float = float(lower), float(upper)
            if Decimal.from_float(lower_float) > lower:
                lower_float = math.nextafter(lower_float, -math.inf)
            if Decimal.from_float(upper_float) < upper:
                upper_float = math.nextafter(upper_float, math.inf)
            bounds.append([lower_float, upper_float])
        return percentages, bounds
