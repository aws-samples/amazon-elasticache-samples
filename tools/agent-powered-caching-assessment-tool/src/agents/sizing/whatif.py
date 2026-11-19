"""
What-if sizing: the Sizing & Cost agent run again with the caller's settings.

It reads a saved analysis and collection and changes nothing. The job's own sizing
stays as the job produced it, so a report and a what-if never silently disagree.
The baseline is the job's recorded sizing, and the settings that a what-if does not
name start from the constants recorded there (the defaults, when the job has none).

Spec: .kiro/specs/ux-api (UX-25 to UX-28).
"""

import dataclasses

from src.agents.sizing.sac_agent import size_and_cost
from src.contracts.api_models import SizingSummary, WhatIfChange, WhatIfResult
from src.contracts.cache_sizing_output import CacheSizingOutput
from src.tools.analysis.cache_sizing import SizingParameters

_FIELDS = {f.name for f in dataclasses.fields(SizingParameters)}


def baseline_parameters(baseline: dict | None) -> SizingParameters:
    """The sizing constants a job used, from its recorded sizing.

    Args:
        baseline: The job's sizing block, or None.

    Returns:
        The recorded constants, with the defaults for any that are missing or not
        usable.
    """
    recorded = (baseline or {}).get("parameters") or {}
    defaults = SizingParameters()
    kept = {}
    for name in _FIELDS:
        value = recorded.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue  # missing, or not a number: use the default
        want = type(getattr(defaults, name))
        if want is int and value != int(value):
            continue
        kept[name] = want(value)
    return SizingParameters(**kept)


def apply_overrides(base: SizingParameters, overrides: dict) -> SizingParameters:
    """The base constants with the overrides applied. None means keep the base value."""
    changes = {k: v for k, v in overrides.items() if v is not None and k in _FIELDS}
    return dataclasses.replace(base, **changes)


def describe_changes(base: SizingParameters, new: SizingParameters, *, base_region: str,
                     region: str, base_price: bool, price: bool) -> list[WhatIfChange]:
    """Every setting that differs between the job's sizing and the what-if.

    Args:
        base: Constants of the job's sizing.
        new: Constants of the what-if.
        base_region: Region the job was priced in.
        region: Region of the what-if.
        base_price: Whether the job asked for pricing.
        price: Whether the what-if does.

    Returns:
        One entry per difference, in a stable order: the constants, then the region,
        then price.
    """
    out = [WhatIfChange(name=f, baseline=getattr(base, f), scenario=getattr(new, f))
           for f in sorted(_FIELDS) if getattr(base, f) != getattr(new, f)]
    if base_region != region:
        out.append(WhatIfChange(name="region", baseline=base_region, scenario=region))
    if base_price != price:
        out.append(WhatIfChange(name="price", baseline=base_price, scenario=price))
    return out


def run_whatif(job_id: str, analysis: dict, collection: dict, baseline: dict | None, *,
               region: str, price: bool, base_region: str, base_price: bool,
               overrides: dict, pricing=None, session=None) -> WhatIfResult:
    """Size again with the caller's settings and set the result beside the job's own.

    Args:
        job_id: The job.
        analysis: The saved analysis (analysis.json).
        collection: The saved collection (output.json).
        baseline: The job's sizing block, or None when sizing did not run.
        region: Region to price in.
        price: Whether to call the AWS Pricing API.
        base_region: Region the job was priced in.
        base_price: Whether the job asked for pricing.
        overrides: Sizing constants to change; None values are ignored.
        pricing: Optional pricing client, for tests.
        session: Optional boto3 Session.

    Returns:
        The scenario, its headline figures, the baseline's, and the differences.
        Never raises on a pricing failure: cost is marked unavailable instead.

    Raises:
        ValueError: when the saved analysis is not a valid v2 analysis.
    """
    base = baseline_parameters(baseline)
    new = apply_overrides(base, overrides)
    scenario = size_and_cost(analysis, collection, region=region, params=new, price=price,
                             pricing=pricing, session=session)
    return WhatIfResult(
        job_id=job_id, region=region, priced=price,
        baseline=SizingSummary.from_sizing(CacheSizingOutput.model_validate(baseline))
        if baseline else None,
        scenario_summary=SizingSummary.from_sizing(scenario),
        scenario=scenario,
        changes=describe_changes(base, new, base_region=base_region, region=region,
                                 base_price=base_price, price=price),
    )
