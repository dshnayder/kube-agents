"""Which clusters the analysis leaves out, and why.

`kage-management` does little beyond occasional tests of pull-request changes, so its
near-idle series say nothing about a cluster under real load and flatter every score. The
evaluation hosts, which every presubmit and nightly deploys into, carry that load. The
forecasts still cover every collected series; the reports drop the excluded ones.
"""

EXCLUDED_CLUSTERS = {"agentic-harness-demo/kage-management"}


def included(series_id):
    """True unless the series (id `project/cluster/...`) belongs to an excluded cluster."""
    return "/".join(str(series_id).split("/")[:2]) not in EXCLUDED_CLUSTERS
