"""Frozen window policy shared by the trusted host and input-only worker."""
from datetime import timedelta

from ...contracts import ContractError
from ..contracts import timestamp

DISCOVERY_WINDOW = 'first-seen-posted-24h-v1'
POSTED_WINDOW = 'posted-at-v1'


def window_policy(packet):
    # Packets saved before the discovery policy retain their original semantics.
    policy = packet.get('window_policy', POSTED_WINDOW)
    if policy not in (DISCOVERY_WINDOW, POSTED_WINDOW):
        raise ContractError('unsupported old-method window policy')
    return policy


def eligible(row, start, end, policy=DISCOVERY_WINDOW):
    if policy not in (DISCOVERY_WINDOW, POSTED_WINDOW):
        raise ContractError('unsupported old-method window policy')
    if row.get('closed_at') or not start < end:
        return False
    try:
        posted = timestamp(row.get('posted_at'))
        if policy == POSTED_WINDOW:
            return start < posted <= end
        seen = timestamp(row.get('first_seen'))
    except ContractError:
        return False
    return start < seen <= end and timedelta(0) <= seen - posted <= timedelta(hours=24)
