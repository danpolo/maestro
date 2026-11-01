"""Immutable owner family facts and same-backend successor profiles.

No model-name parsing and no reads of the moving owner state during a task.
"""
from copy import deepcopy
from dataclasses import replace
from maestro.model_policy import PolicyError


def facts(policy):
    return policy['source'].get('class_metadata', []) if policy else []


def resolve_choice(policy, backend, choice):
    rows = [m for m in facts(policy) if m['backend_id'] == backend]
    identities = [m for m in rows if m['model_id'] == choice or m['family'].casefold() == choice.casefold() or choice in m.get('aliases', [])]
    if not identities: return choice
    families = {m['family'] for m in identities}
    if len(families) != 1: raise PolicyError('ambiguous model class choice')
    eligible = {r['model_id'] for r in policy['routes'] if r['backend_id'] == backend}
    current = [m for m in rows if m['family'] in families and m['model_id'] in eligible]
    if not current: raise PolicyError(f'{backend}:{choice}: model class has no eligible version')
    return max(current, key=lambda m: tuple(m['version']))['model_id']


def profile(policy, backend, model, base):
    from maestro.backends.catalog import ModelProfile, ContextLimits
    record = next((p for p in policy['source'].get('inherited_profiles', [])
                   if p['backend_id'] == backend and p['model_id'] == model), None)
    if record is None:
        result = base.get('model_profiles', {}).get(model)
    else:
        context = ContextLimits(**record['context_limits'])
        result = ModelProfile(model, context_limits=context, usage_pool_id=record.get('usage_pool_id'))
    owner = next((m for m in facts(policy) if m['backend_id'] == backend
                  and model in [m['model_id'], *m.get('aliases', [])]), None)
    if result is not None and owner is not None:
        fields = {key: owner[key] for key in ('canonical_model_id', 'model_family') if key in owner}
        if 'effort_variants' in owner:
            from maestro.backends.catalog import CANONICAL_EFFORTS
            fields['effort_variants'] = {k: v for k, v in owner['effort_variants'].items()
                                        if k in CANONICAL_EFFORTS and owner['set']}
        result = replace(result, **fields)
    return result


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def validate_rows(rows):
    if not isinstance(rows, list):
        raise PolicyError('invalid owner class metadata collection')
    import math
    from maestro.backends.catalog import MODEL_STRENGTHS, CANONICAL_EFFORTS
    seen = set()
    identities = {}
    for row in rows:
        if (not isinstance(row, dict) or not all(_text(row.get(k))
            for k in ('backend_id', 'model_id', 'family')) or not isinstance(row.get('version'), list)
            or not row['version'] or any(type(v) is not int or v < 0 for v in row['version'])
            or not isinstance(row.get('aliases', []), list)
            or not all(_text(alias) for alias in row.get('aliases', []))
            or type(row.get('set')) is not bool
            or row['set'] and (type(row.get('enforced_window')) is not int or row['enforced_window'] <= 0)):
            raise PolicyError('invalid owner class metadata')
        if any(key in row and not _text(row[key])
               for key in ('canonical_model_id', 'model_family')):
            raise PolicyError('invalid owner canonical identity')
        variants = row.get('effort_variants')
        if 'effort_variants' in row and (
            not isinstance(variants, dict)
            or any(k not in (*CANONICAL_EFFORTS, 'thinking') or not _text(v)
                   for k, v in variants.items())
            or len(set(variants.values())) != len(variants)
        ):
            raise PolicyError('invalid owner effort variants')
        grade = row.get('routing_profile')
        if grade is not None and (not isinstance(grade, dict)
            or grade.get('strength') not in MODEL_STRENGTHS
            or type(grade.get('relative_cost')) not in (int, float)
            or not math.isfinite(grade['relative_cost']) or grade['relative_cost'] <= 0
            or type(grade.get('max_output_tokens')) is not int or grade['max_output_tokens'] <= 0
            or 'native_window_tokens' in grade and (type(grade['native_window_tokens']) is not int
                                                    or grade['native_window_tokens'] <= 0)):
            raise PolicyError('invalid owner routing profile')
        reported = row.get('provider_limits')
        if reported is not None and (not isinstance(reported, dict) or not reported
            or not set(reported) <= {'native_window_tokens', 'max_output_tokens'}
            or any(type(v) is not int or v <= 0 for v in reported.values())):
            raise PolicyError('invalid owner provider limits')
        key = row['backend_id'], row['model_id']
        if key in seen:
            raise PolicyError('duplicate owner class metadata')
        seen.add(key)
        for identity in [row['model_id'], row['family'], *row.get('aliases', [])]:
            identity_key = row['backend_id'], identity.casefold()
            previous = identities.setdefault(identity_key, row['family'])
            if previous != row['family']:
                raise PolicyError('ambiguous owner class identity or alias')


def validate_facts(policy):
    import math
    from maestro.backends.catalog import MODEL_STRENGTHS
    rows = facts(policy)
    validate_rows(rows)
    profiles = policy['source'].get('inherited_profiles', [])
    if not isinstance(profiles, list):
        raise PolicyError('invalid inherited model profile collection')
    by_key = {(m['backend_id'], m['model_id']): m for m in rows}
    seen = set()
    for row in profiles:
        if not isinstance(row, dict):
            raise PolicyError('invalid inherited model profile')
        key = row.get('backend_id'), row.get('model_id')
        cost = row.get('relative_cost')
        if (not all(_text(v) for v in key) or key not in by_key or key in seen
            or row.get('strength') not in MODEL_STRENGTHS
            or type(cost) not in (int, float) or not math.isfinite(cost) or cost <= 0
            or 'predecessor_id' not in row
            or row['predecessor_id'] is not None and not _text(row['predecessor_id'])
            or row.get('usage_pool_id') is not None and not _text(row['usage_pool_id'])):
            raise PolicyError('invalid inherited model profile')
        seen.add(key)
        previous = by_key.get((key[0], row['predecessor_id']))
        current = by_key[key]
        if row['predecessor_id'] is not None and (previous is None or previous['family'] != current['family']):
            raise PolicyError('profile inheritance requires same backend and class')
        context = row.get('context_limits')
        if (not isinstance(context, dict)
            or set(context) != {'max_input_tokens', 'max_output_tokens', 'effective_context_ceiling'}
            or any(type(v) is not int or v <= 0 for v in context.values())
            or context['effective_context_ceiling'] > context['max_input_tokens']
            or not current['set'] or context['max_input_tokens'] > current['enforced_window']):
            raise PolicyError('invalid inherited native context window')
