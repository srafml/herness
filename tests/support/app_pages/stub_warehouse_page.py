"""Test-only page script with ``needs_warehouse=True`` (IT09-11 warehouse branch, T09-15).

Stands in for the warehouse pages until T09-16 - T09-18 add them to ``APP_PAGES``.
"""

from __future__ import annotations

import streamlit as st

from app.common.widgets import PageContext, page


def body(ctx: PageContext) -> None:
    st.subheader("Stub warehouse page")
    st.text(f"build {ctx.build_id}")


page("Stub", body)
