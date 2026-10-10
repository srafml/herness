"""Child process of the render crash test (impl 09 FT09-03; T09-11).

Run as ``python -m tests.support.render_kill <data_root> <run_id> [kill]`` from the repository
root with ``HERNESS_ENV=test`` and ``HERNESS_PATHS__DATA=<data_root>`` (the parent's seeded
``report_env``). It renders ``<run_id>`` to HTML and Markdown. With ``kill`` the process ends
itself at the second ``os.replace``: ``report.html`` is complete under its final name, the
Markdown bytes sit fsynced in their ``*.tmp`` file and no ``manifest.json`` exists (Windows
``TerminateProcess`` exit 15, POSIX SIGKILL). Without it one line ``RENDER_RESULT <json>``.
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

RESULT_PREFIX: Final = "RENDER_RESULT "
KILLED_MARK: Final = "render_kill.after_first_replace"
_USAGE: Final = "usage: python -m tests.support.render_kill <data_root> <run_id> [kill]\n"


def _die_at_second_replace() -> None:
    """Patch `os.replace` (``render._write_file`` calls it through ``os``)."""
    real = os.replace
    done: list[str] = []

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if done:
            sys.stderr.write(KILLED_MARK + "\n")
            sys.stderr.flush()
            os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))
        real(src, dst)
        done.append(str(dst))

    os.replace = replace


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: 0 after printing the result line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (2, 3) or (len(args) == 3 and args[2] != "kill"):
        sys.stderr.write(_USAGE)
        return 2
    from tests.support.report_env import NOW, install_redactor  # noqa: PLC0415 - child only

    from herness.reports.render import render_run  # noqa: PLC0415
    from herness.store import ops  # noqa: PLC0415

    ops.reset_connections(path=Path(args[0]) / "ops.sqlite")
    install_redactor()
    if len(args) == 3:
        _die_at_second_replace()
    manifest = render_run(args[1], ["html", "md"], now=NOW)
    sys.stdout.write(RESULT_PREFIX + manifest.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
