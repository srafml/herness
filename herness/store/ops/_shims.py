"""The two impl 08 functions ``run_write`` calls (impl 02 U02-38), as module attributes.

``retry_call`` is ``herness.core.resilience.retry_call`` (T08-07) and ``fault_point`` is
``herness.core.resilience.fault_point`` (T08-08). ``core`` (which never changes) calls both
through this module's attributes, so tests monkeypatch them here; the module is kept for that.
"""

from __future__ import annotations

from herness.core.resilience import fault_point, retry_call

__all__ = ("fault_point", "retry_call")  # re-exports of the real hooks (T08-07, T08-08)
