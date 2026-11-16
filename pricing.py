"""Standard API equivalent USD per million tokens; verified 2026-11-09.

Sources: https://developers.openai.com/api/docs/models/<exact-model>.md
Reference only: no subscription debit, service-tier or tool-call charges.
"""
RATES = {
    "gpt-6-astra": (10, 1, 12.5, 50),
    "gpt-6-sol": (2, .2, 2.5, 10),
    "gpt-6-luna": (.1, .01, .125, .5),
    "gpt-6.1-sol": (2, .1, 2.5, 10),
    "gpt-6.1-terra": (1, .05, 1.25, 5),
}


def cost_usd(model, usage):
    rates = RATES.get(model)
    if rates is None:
        return None
    inp, cached, write, out = (usage.get(k, 0) for k in (
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens"))
    long = inp > 272000
    return (max(0, inp-cached-write)*rates[0]*(2 if long else 1)
            + cached*rates[1]*(2 if long else 1)
            + write*rates[2]*(2 if long else 1)
            + out*rates[3]*(1.5 if long else 1)) / 1_000_000
