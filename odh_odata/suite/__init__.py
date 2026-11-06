"""Test generation and execution.

Three layers, each feeding the next:
  1. metadata-driven  - every set/property/navigation/operation gets structural tests ($select, $orderby, $expand, keys...)
  2. data-driven      - sampled rows supply real values for $filter (every operator/function valid for the type),
                        combinations, key lookups, nested expands and navigation filters
  3. crawl-driven     - follow navigation links entity -> related entity -> its related entities (depth N), checking
                        that everything reachable is addressable, type-correct and referentially consistent
Every returned payload is validated against the schema, and every $filter result is re-evaluated locally.
"""
from .core import FAIL, INFO, PASS, RANK, SKIP, WARN, Ctx, Expr, Exprs, Result, compress, pick, q, qkey
from .runner import Runner

__all__ = ["FAIL", "INFO", "PASS", "RANK", "SKIP", "WARN", "Ctx", "Expr", "Exprs", "Result", "Runner", "compress", "pick", "q", "qkey"]
