"""Bounded complete permutations; never alter selected membership or judgments."""
from ..contracts import ContractError
from .contracts import exact, integer, text

MAX_ORDER_ITEMS = 6000


def order_entries(args):
    exact(args, ('ordinals', 'groups'), ('ordinals',))
    ordinals = args['ordinals']
    if not isinstance(ordinals, list) or len(ordinals) > MAX_ORDER_ITEMS:
        raise ContractError('complete order requires at most 6000 ordinals')
    for ordinal in ordinals:
        integer(ordinal, 'ordinal', 1)
    if len(set(ordinals)) != len(ordinals):
        raise ContractError('complete order ordinals must be unique')
    groups = args.get('groups', [])
    if not isinstance(groups, list) or len(groups) > MAX_ORDER_ITEMS:
        raise ContractError('invalid complete order groups')
    memberships, labels = {}, {}
    selected = set(ordinals)
    for group in groups:
        exact(group, ('id', 'label', 'ordinals'), ('id', 'label', 'ordinals'))
        value = {'id': text(group['id'], 'related group id', 100),
                 'label': text(group['label'], 'related group label', 200)}
        if value['id'] in labels:
            raise ContractError('complete order group ids must be unique')
        labels[value['id']] = value['label']
        members = group['ordinals']
        if not isinstance(members, list) or not members or len(members) > MAX_ORDER_ITEMS:
            raise ContractError('invalid complete order group membership')
        for ordinal in members:
            integer(ordinal, 'group ordinal', 1)
            if ordinal not in selected or ordinal in memberships:
                raise ContractError('group members must occur exactly once in the complete order')
            memberships[ordinal] = value
    return [{'ordinal': ordinal, 'position': position,
             'related_group': memberships.get(ordinal)}
            for position, ordinal in enumerate(ordinals, 1)]


def submit_order(authority, con, grant, args):
    """Reuse per-item receipts and the existing seal in one rollback boundary."""
    from .authority import ReviewAuthorizationError
    if grant['kind'] != 'finalizer':
        raise ReviewAuthorizationError('complete ordering requires a finalizer assignment')
    entries = order_entries(args)
    calibration = authority._finalizer_batch_basis(con, grant)
    if {entry['ordinal'] for entry in entries} != set(calibration[-1]):
        raise ReviewAuthorizationError('complete order must contain every selected posting exactly once')
    for entry in entries:
        authority._finalizer_call(con, grant, 'calibrate', entry, _calibration=calibration)
    return authority._finalizer_call(con, grant, 'finalize', {})
