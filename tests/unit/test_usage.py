"""Tests for the Usage cache-hit metrics.

These exist so we notice if a future refactor of `Usage` quietly changes the
denominator and breaks the cache-hit-pct log line in adjudicator.done_all,
which is the canary for whether prompt-caching is actually paying off.
"""

from app.llm.client import Usage


def test_cache_hit_rate_zero_when_no_cache_traffic():
    u = Usage(input_tokens=1000, output_tokens=200)
    assert u.cache_hit_rate == 0.0
    assert u.cacheable_input_tokens == 0


def test_cache_hit_rate_handles_all_cache_reads():
    u = Usage(input_tokens=100, cache_read_tokens=900, cache_creation_tokens=0)
    assert u.cache_hit_rate == 1.0
    assert u.cacheable_input_tokens == 900
    assert u.total_input_tokens == 1000


def test_cache_hit_rate_handles_creation_only():
    """First leaf in a batch pays the cache-creation cost — hit rate is 0
    against that traffic until later leaves replay the prefix."""
    u = Usage(input_tokens=100, cache_read_tokens=0, cache_creation_tokens=500)
    assert u.cache_hit_rate == 0.0
    assert u.cacheable_input_tokens == 500


def test_cache_hit_rate_mixed():
    u = Usage(cache_read_tokens=750, cache_creation_tokens=250)
    assert u.cache_hit_rate == 0.75


def test_usage_add_preserves_metric_math():
    """Aggregating per-leaf Usage objects must give the same rate as one
    Usage built from the summed counters — this is how the adjudicator
    reports a batch-level number."""
    a = Usage(cache_read_tokens=300, cache_creation_tokens=200)
    b = Usage(cache_read_tokens=600, cache_creation_tokens=100)
    a.add(b)
    assert a.cache_read_tokens == 900
    assert a.cache_creation_tokens == 300
    assert a.cache_hit_rate == 0.75
