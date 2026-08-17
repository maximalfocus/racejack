"""The deliberately vulnerable application. **Local educational material — never deploy this.**

Everything in here is the same storefront the secure application serves, with the same routes, the
same credentials, and the same payloads. One thing differs, and it is the whole point: the check and
the act are two separate steps, so the fact a request checked is already stale by the time it acts
on it.

Starting this application requires two deliberate actions — an opt-in Compose profile *and* the
explicit acknowledgement ``ALLOW_VULNERABLE_DEMO=true``. Neither alone is enough.
"""
