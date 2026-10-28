"""Generate secure random passwords locally with Python's `secrets` module."""
import secrets
import string

SYMBOLS = "!@#$%^&*()-_=+[]{};:,.?"


def _int(value, default, low, high):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def run(arguments, context):
    length = _int(arguments.get("length"), 20, 8, 128)
    count = _int(arguments.get("count"), 1, 1, 10)
    symbols = arguments.get("include_symbols", True)
    if isinstance(symbols, str):
        symbols = symbols.strip().lower() not in ("false", "no", "0")
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
    if symbols:
        pools.append(SYMBOLS)
    alphabet = "".join(pools)
    rng = secrets.SystemRandom()

    def one():
        # At least one character from every pool, the rest from the whole alphabet, then shuffled.
        chars = [secrets.choice(pool) for pool in pools]
        chars += [secrets.choice(alphabet) for _ in range(length - len(pools))]
        rng.shuffle(chars)
        return "".join(chars)

    return {
        "passwords": [one() for _ in range(count)],
        "length": length,
        "include_symbols": bool(symbols),
        "note": "Generated locally with Python's secrets module.",
    }
