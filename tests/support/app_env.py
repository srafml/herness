"""Dashboard test helpers (impl 09 §11, T09-13): UI configs, fake services, AppTest scripts.

``fake_services`` stands in for ``app.common.bootstrap.get_services`` (only ``cfg.security.ui``
and ``roster`` are read by ``current_actor``); ``actor_script`` is an AppTest script that shows
the session actor as ``role|user_ref|display``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from types import SimpleNamespace

from app.common.auth import build_roster
from herness.core.settings import ExposeConfig, RolesConfig, UiConfig

USER_REF_KEY = b"app-unit-test-key-not-secret"  # pragma: allowlist secret
PROXY = "127.0.0.1"


@contextmanager
def preserved_main() -> Iterator[None]:
    """Restore ``sys.modules["__main__"]`` after AppTest runs.

    AppTest leaves its temporary script installed as ``__main__``; a later ``spawn`` child
    process would re-run that script (seen in test_jobs_gpu_lock).
    """
    saved = sys.modules.get("__main__")
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["__main__"] = saved


def ui_config(
    *,
    exposed: bool = False,
    bind: str = "127.0.0.1",
    proxy: str | None = PROXY,
    admins: Sequence[str] = (),
    reviewers: Sequence[str] = (),
    default_role: str = "viewer",
) -> UiConfig:
    """A ``security.ui`` section; exposure through ``proxy`` when ``exposed``."""
    roles = RolesConfig.model_validate(
        {"admins": list(admins), "reviewers": list(reviewers), "default_role": default_role}
    )
    expose = ExposeConfig(enabled=exposed, trusted_proxy=proxy if exposed else None)
    return UiConfig(bind=bind, expose=expose, roles=roles)


def fake_services(ui: UiConfig) -> SimpleNamespace:
    """The two ``AppServices`` fields ``current_actor`` reads."""
    cfg = SimpleNamespace(security=SimpleNamespace(ui=ui))
    return SimpleNamespace(cfg=cfg, roster=build_roster(ui.roles, USER_REF_KEY))


def actor_script() -> None:
    """AppTest script: resolve the session actor and show it."""
    import streamlit as st  # noqa: PLC0415 - AppTest runs this function's body as a script

    from app.common.auth import current_actor  # noqa: PLC0415

    actor = current_actor()
    st.text(f"{actor.role}|{actor.user_ref}|{actor.display}")
