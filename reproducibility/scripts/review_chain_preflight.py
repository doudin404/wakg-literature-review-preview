"""Collect all marker authority defects before mutating records or exporting."""
from vector_curves import digest


def inspect(plan, acceptance, coverage, implementation_sha256):
    subject = digest(plan)
    checks = {
        'merge_verdict': acceptance.get('verdict') == 'PASS' and not acceptance.get('defects'),
        'merge_message': bool(acceptance.get('message_id')),
        'merge_plan': acceptance.get('plan_sha256') == subject,
        'merge_implementation': acceptance.get('implementation_sha256') == implementation_sha256,
        'coverage_verdict': coverage.get('verdict') == 'PASS' and not coverage.get('defects'),
        'coverage_message': bool(coverage.get('message_id')),
        'coverage_plan': coverage.get('plan_sha256') == subject,
        'coverage_scope': coverage.get('coverage_scope') == 'complete_plotted_markers',
    }
    return {'status': 'PASS' if all(checks.values()) else 'REQUIRES_REVIEW_CHAIN_REPAIR',
            'plan_sha256': subject, 'acceptance_sha256': digest(acceptance),
            'coverage_sha256': digest(coverage), 'checks': checks,
            'defects': [key for key, passed in checks.items() if not passed],
            'publication_authorized': False}
