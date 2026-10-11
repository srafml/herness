"""Private sibling of `tests.support.builds` (T11-39): the clock pin of a fixture build.

The enrichment windows (`decisions.clustering.window_days`, ...) are `clock.now()`-relative
by design while the synthetic data ends on the fixed `DEFAULT_END`, so a build made on another
day clusters another slice. `pinned_clock()` makes `herness.core.time.now` return the synth end
+ 1 day (the as-of `SynthEnv` scores at) for the block and restores the real function on every
path. Only `now` is pinned (not freezegun, not `sleep` / `monotonic`): the build lock poll,
the loopback stub servers and HTTP timeouts need real time, and nothing in the build loops on
a `clock.now()` deadline. Patching the module attribute reaches every caller that uses
`from herness.core import time as clock`, in every thread; a spawned subprocess would not see
it (the build spawns none that read the clock).
"""

from __future__ import annotations

import contextlib
import datetime
from collections.abc import Iterator
from typing import Final

from herness.core import time as clock
from tools.synth._cli_subcommands import DEFAULT_END

PIN: Final = datetime.datetime.combine(DEFAULT_END, datetime.time(), datetime.UTC) + (
    datetime.timedelta(days=1)
)


@contextlib.contextmanager
def pinned_clock() -> Iterator[datetime.datetime]:
    """`clock.now()` returns `PIN` inside the block; the previous `now` is restored in `finally`."""
    saved = clock.now
    clock.now = lambda: PIN  # type: ignore[method-assign]
    try:
        yield PIN
    finally:
        clock.now = saved  # type: ignore[method-assign]
