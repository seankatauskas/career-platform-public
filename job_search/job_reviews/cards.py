"""Worker-safe shared visible recommendation validation."""
from ..contracts import ContractError

def visible_explanation(assessment):
    """Material caveats are durable card text, not hidden evidence-panel content."""
    labels = {'close': 'Close fit', 'slight_stretch': 'Slight stretch',
              'bigger_stretch': 'Bigger stretch', 'broad_only': 'Broad only'}
    pieces = [labels.get(assessment['decision'], assessment['decision']) + ': ' + assessment['explanation']]
    condition = assessment.get('eligibility_condition')
    if condition and condition.casefold() not in pieces[0].casefold():
        pieces.append('Eligibility: ' + condition)
    if assessment.get('category') == 'alternative':
        pieces.append('Career alternative; outside the main software-building direction.')
    for name, observations in (('Gap', assessment.get('gaps', [])), ('Caveat', assessment.get('unknowns', []))):
        for observation in observations:
            if observation.casefold() not in ' '.join(pieces).casefold():
                pieces.append(name + ': ' + observation)
    next_step = {'apply': 'Apply', 'clarify': 'Clarify the condition before applying',
                 'explore': 'Explore whether this path is appealing'}.get(assessment.get('next_step'))
    if next_step:
        pieces.append('Next step: ' + next_step)
    result = ' '.join(pieces)
    # The curated-list API has always required a <=2,000 character explanation.
    # Never silently truncate caveats. Reviewer can make the original prose concise.
    if len(result) > 2000:
        raise ContractError('visible explanation exceeds 2000 characters; shorten prose while preserving material caveats')
    return result
