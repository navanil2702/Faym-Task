"""Human-like interaction primitives.

Bot detection on Indian e-commerce sites keys on three things far more than on
the browser fingerprint: inhuman timing, inhuman pointer paths, and inhuman
throughput. Everything here targets those.

Randomness is drawn from a seeded ``random.Random`` per session so a run can be
reproduced when debugging a failure, while still varying between sessions.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import Locator, Page


@dataclass
class Pacing:
    """Timing envelope for one session. All values in seconds."""

    think_min: float = 0.7
    think_max: float = 2.6
    """Pause before acting on a freshly loaded view - reading time."""

    key_min: float = 0.045
    key_max: float = 0.185
    """Per-character typing delay."""

    between_items_min: float = 12.0
    between_items_max: float = 38.0
    """Cool-off between consecutive return flows on the same account."""

    between_orders_min: float = 25.0
    between_orders_max: float = 70.0

    long_break_every: int = 6
    """After this many line items, take a much longer break."""

    long_break_min: float = 120.0
    long_break_max: float = 300.0

    max_items_per_session: int = 25
    """Hard cap; beyond this the session stops and asks to be resumed later."""


@dataclass
class Human:
    """Wraps a Playwright page with human-shaped timing and pointer movement."""

    page: Page
    pacing: Pacing = field(default_factory=Pacing)
    seed: Optional[int] = None
    _rng: random.Random = field(init=False)
    _pointer: tuple[float, float] = field(init=False, default=(400.0, 300.0))
    _actions: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    # ------------------------------------------------------------------ timing

    def _sleep(self, seconds: float) -> None:
        self.page.wait_for_timeout(max(0.0, seconds) * 1000)

    def think(self, scale: float = 1.0) -> None:
        """Pause as though reading the current view."""
        self._sleep(self._rng.uniform(self.pacing.think_min, self.pacing.think_max) * scale)

    def micro(self) -> None:
        """Short hesitation between sub-actions in the same view."""
        self._sleep(self._rng.uniform(0.12, 0.45))

    def between_items(self) -> None:
        self._actions += 1
        if self.pacing.long_break_every and self._actions % self.pacing.long_break_every == 0:
            self._sleep(
                self._rng.uniform(self.pacing.long_break_min, self.pacing.long_break_max)
            )
        else:
            self._sleep(
                self._rng.uniform(self.pacing.between_items_min, self.pacing.between_items_max)
            )

    def between_orders(self) -> None:
        self._sleep(
            self._rng.uniform(self.pacing.between_orders_min, self.pacing.between_orders_max)
        )

    # ----------------------------------------------------------------- pointer

    def _move_to(self, x: float, y: float) -> None:
        """Move the pointer along an eased, slightly curved path.

        A straight-line jump in a single mouse event is one of the cheapest bot
        tells there is; real pointers arrive on a curve, in many steps, and
        decelerate near the target.
        """
        start_x, start_y = self._pointer
        distance = math.hypot(x - start_x, y - start_y)
        steps = max(6, min(28, int(distance / 18) + self._rng.randint(4, 9)))

        # Control point offset perpendicular to the path gives a gentle arc.
        bow = self._rng.uniform(-0.22, 0.22) * distance
        mid_x = (start_x + x) / 2 - bow * (y - start_y) / (distance or 1)
        mid_y = (start_y + y) / 2 + bow * (x - start_x) / (distance or 1)

        for step in range(1, steps + 1):
            t = step / steps
            eased = t * t * (3 - 2 * t)  # smoothstep: accelerate then settle
            px = (1 - eased) ** 2 * start_x + 2 * (1 - eased) * eased * mid_x + eased**2 * x
            py = (1 - eased) ** 2 * start_y + 2 * (1 - eased) * eased * mid_y + eased**2 * y
            jitter = 0.9 if step < steps else 0.0
            self.page.mouse.move(
                px + self._rng.uniform(-jitter, jitter),
                py + self._rng.uniform(-jitter, jitter),
            )
            self._sleep(self._rng.uniform(0.004, 0.017))

        self._pointer = (x, y)

    def click(self, locator: Locator, *, settle: bool = True) -> None:
        """Scroll into view, drift the pointer over, dwell, then click."""
        locator.scroll_into_view_if_needed(timeout=15000)
        self._sleep(self._rng.uniform(0.25, 0.8))

        box = locator.bounding_box()
        if box:
            # Aim off-centre; humans do not hit the exact middle of a button.
            target_x = box["x"] + box["width"] * self._rng.uniform(0.32, 0.68)
            target_y = box["y"] + box["height"] * self._rng.uniform(0.34, 0.66)
            self._move_to(target_x, target_y)
            self._sleep(self._rng.uniform(0.08, 0.28))  # hover dwell
            self.page.mouse.down()
            self._sleep(self._rng.uniform(0.045, 0.135))  # press duration
            self.page.mouse.up()
        else:
            locator.click(timeout=15000)

        if settle:
            self._sleep(self._rng.uniform(0.4, 1.2))

    def type_text(self, locator: Locator, text: str) -> None:
        """Type character by character, with realistic pauses and rhythm."""
        self.click(locator, settle=False)
        self._sleep(self._rng.uniform(0.15, 0.4))
        for index, char in enumerate(text):
            self.page.keyboard.type(char)
            delay = self._rng.uniform(self.pacing.key_min, self.pacing.key_max)
            # Occasional longer pause, as when glancing at the source.
            if index and self._rng.random() < 0.08:
                delay += self._rng.uniform(0.25, 0.9)
            self._sleep(delay)

    def scroll(self, amount: Optional[int] = None) -> None:
        """Scroll in a few uneven bursts rather than one jump."""
        total = amount if amount is not None else self._rng.randint(240, 620)
        remaining = total
        while abs(remaining) > 20:
            burst = int(math.copysign(min(abs(remaining), self._rng.randint(90, 210)), remaining))
            self.page.mouse.wheel(0, burst)
            remaining -= burst
            self._sleep(self._rng.uniform(0.06, 0.24))
        self._sleep(self._rng.uniform(0.2, 0.7))

    def browse_idle(self) -> None:
        """Aimless movement and scrolling, to break up mechanical sequences."""
        for _ in range(self._rng.randint(1, 3)):
            viewport = self.page.viewport_size or {"width": 1280, "height": 800}
            self._move_to(
                self._rng.uniform(80, viewport["width"] - 80),
                self._rng.uniform(80, viewport["height"] - 80),
            )
            self._sleep(self._rng.uniform(0.2, 0.9))
        if self._rng.random() < 0.6:
            self.scroll(self._rng.randint(-260, 380))
