"""Core PerfEval package."""

from perfeval.core.compare import compare_run_groups, compare_summaries
from perfeval.core.summary import aggregate_summary, summarize_timeline

__all__ = [
    "aggregate_summary",
    "compare_run_groups",
    "compare_summaries",
    "summarize_timeline",
]
