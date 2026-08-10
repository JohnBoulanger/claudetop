"""Model pricing, in $ per 1M tokens.

One table, shared by the session list and the spending panel, so the two can
never drift. Cache reads bill at ~0.1x the input rate and 5-minute cache
writes at ~1.25x, which is what the multipliers below encode.
"""

# model id prefix -> (input $/1M, output $/1M)
PRICING = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-3-7-sonnet": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
}
DEFAULT_RATE = (5.0, 25.0)

CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25


def rate(model):
    """Rates for a model id. Falls back to the longest matching prefix so a
    dated id (claude-opus-5-20260514) still prices correctly."""
    if not model:
        return DEFAULT_RATE
    if model in PRICING:
        return PRICING[model]
    best = None
    for prefix, r in PRICING.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, r)
    return best[1] if best else DEFAULT_RATE


def cost(model, tin=0, tout=0, cache_read=0, cache_write=0):
    """Dollar cost of one message's token usage."""
    ri, ro = rate(model)
    return (tin * ri
            + tout * ro
            + cache_read * ri * CACHE_READ_MULT
            + cache_write * ri * CACHE_WRITE_MULT) / 1e6


def short_model(model):
    """'claude-opus-5' -> 'opus-5'; keeps the table narrow."""
    if not model:
        return "unknown"
    return model[len("claude-"):] if model.startswith("claude-") else model
