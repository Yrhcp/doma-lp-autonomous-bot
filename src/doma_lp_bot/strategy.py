from collections import deque
from decimal import Decimal as D


def buffered_exit(snapshot, buffer_percent):
    """Return exit direction; actual LP status remains tick-based."""
    if snapshot['status'] == 'IN_RANGE': return None
    price = snapshot['price_usdc_per_weth']
    lower = snapshot['lower_price']; upper = snapshot['upper_price']
    if buffer_percent == 0:
        return 'below' if price <= lower else 'above'
    f = D(buffer_percent) / D(100)
    if price <= lower * (1-f): return 'below'
    if price >= upper * (1+f): return 'above'
    return None

class SignalGate:
    def __init__(self, confirm_seconds, cooldown_seconds, max_signals, prior=(), max_gap=90, latched=None):
        self.confirm = confirm_seconds
        self.cooldown = cooldown_seconds
        self.limit = max_signals
        self.max_gap = max_gap
        self.latched = latched
        self.signals = deque(sorted(prior))
        self.reset_observation()

    def reset_observation(self):
        self.since = None
        self.last_block = None
        self.last_time = None
        self.direction = None

    def observe(self, in_range, now, block, direction=None, identity=None, actual_in_range=None):
        # Latch a signalled episode until a verified re-entry, position change or
        # a newly observed exit on the other side. Gaps do not re-arm it.
        if actual_in_range is None: actual_in_range = in_range
        if self.latched:
            old_identity, old_direction = self.latched
            if actual_in_range or (identity is not None and old_identity is not None and identity != old_identity) or (direction is not None and old_direction not in (None, 'unknown', direction)):
                self.latched = None
        # A clock rollback or gap breaks continuity; future signals must not be bypassed.
        while self.signals and self.signals[0] <= now - 86400:
            self.signals.popleft()
        if in_range:
            self.reset_observation()
            return False
        if self.last_time is not None and (now < self.last_time or now-self.last_time > self.max_gap or block <= self.last_block or direction != self.direction):
            self.reset_observation()
        self.last_block = block
        self.last_time = now
        self.direction = direction
        self.identity = identity
        if self.since is None: self.since = now
        if self.latched is not None: return False
        if self.signals and now < self.signals[-1]: return False
        if now-self.since < self.confirm or (self.limit is not None and len(self.signals) >= self.limit): return False
        if self.signals and now-self.signals[-1] < self.cooldown: return False
        return True

    def record(self, now):
        self.signals.append(now)
        self.latched = (self.identity, self.direction or 'unknown')
        self.reset_observation()
