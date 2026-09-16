from typing import Dict, List


def verdict(record: Dict, route_result: Dict, source_result: Dict) -> bool:
    """Compose route + source results into a single is_interchangeable flag.

    Returns True iff route_accepted AND source_equivalent.
    """
    return (
        route_result["verdict"] == "accepted"
        and source_result["verdict"] == "equivalent"
    )


def aggregate(per_record_verdicts: List[bool]) -> int:
    """Return the count of True verdicts across per-record results."""
    return sum(1 for v in per_record_verdicts if v)
