"""D3: process-wide metrics counters + GET /metrics."""

from mantisfetch_common import metrics as m


def test_metrics_incr_and_snapshot():
    m.reset()
    m.incr("distill_calls")
    m.incr("distill_input_chars", 100)
    m.incr("distill_output_chars", 20)
    m.incr("capture_cache_hits", 3)
    m.incr("capture_cache_misses", 1)
    snap = m.snapshot()
    assert snap["distill_calls"] == 1
    assert snap["distill_input_chars"] == 100
    assert snap["ratios"]["distill_output_over_input"] == 0.2
    assert snap["ratios"]["capture_cache_hit_rate"] == 0.75
    m.reset()
    assert m.snapshot()["distill_calls"] == 0


def test_metrics_endpoint(client):
    m.reset()
    m.incr("failover_summary", 2)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["metrics"]["failover_summary"] == 2
