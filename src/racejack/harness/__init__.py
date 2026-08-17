"""The concurrent load harness.

A race is invisible to any sequential test: the same code that oversells a drop to sixty concurrent
buyers passes every functional assertion when its requests arrive one at a time. This package is the
instrument that makes concurrency observable — it generates genuine concurrent load against the
demonstration's own services, records what each request got, and reconciles the resulting ledger
against the invariant.

It measures **correctness under concurrency only**. It is not a load-testing tool, it makes no
throughput or latency claim, and it never will.
"""
