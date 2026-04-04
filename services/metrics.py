"""
Prometheus-compatible metrics for BDS Agent.

Exposes:
  - bds_enricher_jobs_total          (labels: status=completed|failed)
  - bds_enricher_job_duration_seconds
  - bds_enricher_queue_depth         (labels: status=pending|processing|completed|retry)
  - bds_posts_total                  (labels: source=crawl|observation)
  - bds_search_requests_total
  - bds_embedding_duration_seconds
  - bds_http_requests_total          (labels: method, endpoint, status_code)
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Generator

from db import connect_db, ensure_schema, get_database_url


# ── Counter / Gauge helpers (no external dependency required) ──────────────────

class MetricsStore:
    """In-process metrics store — Prometheus-compatible.

    Exposes a /metrics endpoint via prometheus_client or plain text.
    If prometheus_client is installed, we use it; otherwise we use plain counters.
    """

    def __init__(self):
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = {}
        self._labels: dict[str, dict[tuple, float]] = {}

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = self._make_key(name, labels)
        self._counters[key] = self._counters.get(key, 0.0) + value

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        key = self._make_key(name, labels)
        self._gauges[key] = value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = self._make_key(name, labels)
        self._histograms.setdefault(key, []).append(value)

    @contextmanager
    def timer(self, name: str, **labels: str) -> Generator[None, None, None]:
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - start
            self.observe(name, elapsed, **labels)

    def _make_key(self, name: str, labels: dict[str, str]) -> tuple[str, tuple]:
        label_items = tuple(sorted(labels.items()))
        return (name, label_items)

    def _format_labels(self, labels: tuple) -> str:
        if not labels:
            return ""
        return "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}"

    def export(self) -> str:
        """Return Prometheus plain-text exposition format."""
        lines: list[str] = []

        # HELP / TYPE for counters
        counters_seen: set[str] = set()
        for (name, labels), value in sorted(self._counters.items()):
            if name not in counters_seen:
                lines.append(f"# TYPE {name} counter")
                lines.append(f"# HELP {name} no help text")
                counters_seen.add(name)
            lines.append(f"{name}{self._format_labels(labels)} {value}")

        for (name, labels), value in sorted(self._gauges.items()):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"# HELP {name} no help text")
            lines.append(f"{name}{self._format_labels(labels)} {value}")

        for (name, labels), values in sorted(self._histograms.items()):
            lines.append(f"# TYPE {name} histogram")
            lines.append(f"# HELP {name} no help text")
            sorted_vals = sorted(values)
            n = len(sorted_vals)
            p50 = sorted_vals[int(n * 0.5)] if n else 0
            p95 = sorted_vals[int(n * 0.95)] if n else 0
            p99 = sorted_vals[int(n * 0.99)] if n else 0
            lines.append(f"{name}_sum{self._format_labels(labels)} {sum(values)}")
            lines.append(f"{name}_count{self._format_labels(labels)} {len(values)}")
            lines.append(f"{name}_bucket{{le=\"0.05\"}}{self._format_labels(labels)} {sum(1 for v in values if v <= 0.05)}")
            lines.append(f"{name}_bucket{{le=\"0.5\"}}{self._format_labels(labels)} {sum(1 for v in values if v <= 0.5)}")
            lines.append(f"{name}_bucket{{le=\"1.0\"}}{self._format_labels(labels)} {sum(1 for v in values if v <= 1.0)}")
            lines.append(f"{name}_bucket{{le=\"+Inf\"}}{self._format_labels(labels)} {n}")
            del values[:]  # reset histogram after export

        return "\n".join(lines) + "\n"

    def refresh_gauge(self, name: str, **labels: str) -> None:
        """Compute and set a gauge from DB state. No-op on failure."""
        try:
            value = self._query_gauge(name, **labels)
            self.set_gauge(name, value, **labels)
        except Exception:
            pass

    def _query_gauge(self, name: str, **labels: str) -> float:
        if name == "bds_enricher_queue_depth":
            status = labels.get("status", "pending")
            conn = connect_db(get_database_url(None))
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM llm_enrichment_queue WHERE status = %s",
                        (status,)
                    )
                    return float(cur.fetchone()[0] or 0)
            finally:
                conn.close()
        elif name == "bds_posts_total":
            conn = connect_db(get_database_url(None))
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM canonical_posts")
                    return float(cur.fetchone()[0] or 0)
            finally:
                conn.close()
        return 0.0


# Singleton
metrics = MetricsStore()


# ── Convenience wrappers ────────────────────────────────────────────────────────

def inc_enrich_completed() -> None:
    metrics.inc("bds_enricher_jobs_total", status="completed")


def inc_enrich_failed() -> None:
    metrics.inc("bds_enricher_jobs_total", status="failed")


def observe_enrich_duration(seconds: float) -> None:
    metrics.observe("bds_enricher_job_duration_seconds", seconds)


def observe_embedding_duration(seconds: float) -> None:
    metrics.observe("bds_embedding_duration_seconds", seconds)


def inc_search_requests() -> None:
    metrics.inc("bds_search_requests_total")


def inc_http_request(method: str, endpoint: str, status_code: int) -> None:
    metrics.inc("bds_http_requests_total", method=method, endpoint=endpoint, status_code=str(status_code))


@contextmanager
def track_enrich_duration() -> Generator[None, None, None]:
    start = time.monotonic()
    try:
        yield
    finally:
        observe_enrich_duration(time.monotonic() - start)


@contextmanager
def track_embedding_duration() -> Generator[None, None, None]:
    start = time.monotonic()
    try:
        yield
    finally:
        observe_embedding_duration(time.monotonic() - start)
