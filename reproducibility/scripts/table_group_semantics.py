"""Resolve categorical identity only from the bound table-row identity."""
import re


def foaming_agent_from_identity(identity):
    text = str(identity).casefold().translate(str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789'))
    found = {name for name in ('h2o2', 'spc')
             if re.search(r'(?<!\w)' + name + r'(?!\w)', text)}
    return next(iter(found)) if len(found) == 1 else 'foaming_agent'
