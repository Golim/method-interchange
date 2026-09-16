from typing import Dict, List


def evaluate_record(record: Dict) -> Dict:
    """ACCEPTED iff any candidate returns 2xx OR 400.

    Returns:
        {"verdict": "accepted" | "rejected",
         "evidence": {"candidate_status_codes": [int|None, ...]}}
    """
    if record.get("replay_skipped"):
        return {"verdict": "rejected", "evidence": {"candidate_status_codes": []}}
    candidates = record.get("get_candidates", [])
    statuses = [c["get_response"].get("status_code") for c in candidates]
    accepted = any(
        (s is not None) and (200 <= s < 300 or s == 400)
        for s in statuses
    )
    return {
        "verdict": "accepted" if accepted else "rejected",
        "evidence": {"candidate_status_codes": statuses},
    }


def aggregate(per_record_results: List[Dict]) -> Dict:
    """Return {"accepted": int, "rejected": int}."""
    accepted = sum(1 for r in per_record_results if r["verdict"] == "accepted")
    return {"accepted": accepted, "rejected": len(per_record_results) - accepted}
