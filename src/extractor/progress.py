"""
Single-line progress bar with an ETA, for the long offline exports.

These tools routinely run for minutes (a whole-map pack is ~30, the atlas is
~15), and without a bar there is no way to tell a slow step from a hung one.
That matters most on the steps that produce no output until the very end.

Deliberately ASCII (`#` / `-`) rather than block characters: the batch files
run in cmd.exe, whose default code page mangles anything outside it, and a
progress bar that renders as garbage is worse than none.

Usage:

    with Bar(len(cells), "cells") as bar:
        for c in cells:
            ...
            bar.update(1, f"cell {c[0]},{c[1]}")

Writes to stdout so it appears in a batch window, and redraws at most ~10x a
second — updating per item makes a fast loop spend real time on I/O.
"""
import sys, time, shutil


def human_time(seconds):
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


class Bar:
    def __init__(self, total, unit="", label="", width=28, stream=None,
                 min_interval=0.1, enabled=True):
        self.total = max(int(total), 0)
        self.unit = unit
        self.label = label
        self.width = width
        self.stream = stream or sys.stdout
        self.min_interval = min_interval
        # A bar is noise when nothing is watching, and interleaves badly with
        # piped output, so it turns itself off when stdout is not a terminal.
        # An unknown total is NOT a reason to show nothing. This used to
        # require total > 0, so a caller that could not count its work up
        # front printed absolutely nothing for minutes and looked hung.
        # Without a total there is no percentage or ETA to give, so it falls
        # back to a moving spinner with a live count and rate.
        self.indeterminate = self.total <= 0
        self.enabled = enabled and self.stream.isatty()
        self.done = 0
        self.started = time.time()
        self._last_draw = 0.0
        self._last_len = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def update(self, n=1, suffix=""):
        self.done += n
        now = time.time()
        if not self.enabled:
            return
        if now - self._last_draw < self.min_interval \
                and (self.indeterminate or self.done < self.total):
            return
        self._last_draw = now
        self._draw(suffix)

    # A bouncing block, so the line visibly moves even when the count is
    # climbing too slowly to notice.
    _SPIN = "-\\|/"

    def _draw(self, suffix=""):
        elapsed = time.time() - self.started

        if self.indeterminate:
            pos = int(elapsed * 8) % max(self.width - 3, 1)
            bar = "-" * pos + "###" + "-" * (self.width - 3 - pos)
            rate = self.done / elapsed if elapsed > 0.01 else 0.0
            spin = self._SPIN[int(elapsed * 8) % 4]
            line = (f"  {spin} [{bar}] {self.done:,} {self.unit}  "
                    f"{rate:,.0f}/s  {human_time(elapsed)}")
            if suffix:
                line += f"   {suffix}"
            self._emit(line)
            return

        frac = min(self.done / self.total, 1.0) if self.total else 1.0
        filled = int(frac * self.width)
        bar = "#" * filled + "-" * (self.width - filled)

        if self.done > 0 and frac < 1.0:
            eta = f"  eta {human_time(elapsed / self.done * (self.total - self.done))}"
        else:
            eta = f"  {human_time(elapsed)}"

        line = (f"  [{bar}] {frac * 100:5.1f}%  "
                f"{self.done:,}/{self.total:,} {self.unit}{eta}")
        if suffix:
            line += f"   {suffix}"
        self._emit(line)

    def _emit(self, line):

        # Trim to the window so a long suffix cannot wrap; a wrapped line
        # leaves the previous row on screen and the bar appears to scroll.
        cols = shutil.get_terminal_size((100, 25)).columns - 1
        if len(line) > cols:
            line = line[:cols]
        pad = " " * max(self._last_len - len(line), 0)
        self._last_len = len(line)
        self.stream.write("\r" + line + pad)
        self.stream.flush()

    def close(self, summary=""):
        if not self.enabled:
            if summary:
                print(summary)
            return
        self._draw()
        self.stream.write("\n")
        self.stream.flush()
        if summary:
            print(summary)


def step(index, total, title):
    """Banner for one stage of a multi-stage run."""
    print()
    print(f"  === [{index}/{total}] {title} " + "=" * max(0, 46 - len(title)))
