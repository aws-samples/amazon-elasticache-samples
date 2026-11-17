"""The shape of an AWS region name, checked wherever a region reaches a command or a call."""

import re

# A region name such as us-east-1, eu-west-2, us-gov-west-1 or cn-north-1. Anything
# else is refused, so the value cannot carry more than a region into the Pricing API
# call or a shell command.
REGION_PATTERN = re.compile(r"^[a-z]{2}-(?:[a-z]{1,16}-){1,3}[0-9]{1,2}$")
REGION_MAX_LENGTH = 32


def valid_region(region: str) -> bool:
    """Whether a string is shaped like an AWS region name.

    Args:
        region: Candidate region.

    Returns:
        True for names such as us-east-1 or ap-southeast-2. This checks the shape
        only; it does not know which regions exist. A trailing newline is refused.
    """
    # fullmatch: "$" in the pattern would also accept a trailing newline.
    return bool(isinstance(region, str) and len(region) <= REGION_MAX_LENGTH
                and REGION_PATTERN.fullmatch(region))
