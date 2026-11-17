"""The one release number.

The package (pyproject.toml), the collector and the analysis agent all report
this value, so an artifact says which release produced it. The analysis
contract version (cache_analysis_output_v2) and the prompt version (v2_prompts)
keep their own numbers: they change when the output shape or the model's
instructions change, not with every release.

1.0.0 is reserved for the release after threshold calibration.
Spec: .kiro/specs/v2-consolidation (VC-7).
"""

__version__ = "0.4.0"
