from typing import Dict, List


def aggregate(per_record_source_results: List[Dict], records: List[Dict]) -> Dict:
    """source-equivalence rate per content type bucket.

    Returns:
        {
            "json":            {"total": int, "source_equivalent": int, "rate": float},
            "form-urlencoded": {"total": int, "source_equivalent": int, "rate": float},
            "multipart":       {"total": int, "source_equivalent": int, "rate": float},
        }

    Excludes lossy_conversion=true and replay_skipped records.
    rate = source_equivalent / total; 0.0 on empty bucket.
    """
    out = {
        "json": {"total": 0, "source_equivalent": 0},
        "form-urlencoded": {"total": 0, "source_equivalent": 0},
        "multipart": {"total": 0, "source_equivalent": 0},
    }
    for r, se in zip(records, per_record_source_results):
        if r.get("replay_skipped"):
            continue
        if r.get("lossy_conversion"):
            continue
        bucket = r.get("source_content_type_bucket", "unknown")
        if bucket not in out:
            continue
        out[bucket]["total"] += 1
        if se.get("verdict") == "equivalent":
            out[bucket]["source_equivalent"] += 1
    for b in out:
        t = out[b]["total"]
        out[b]["rate"] = (out[b]["source_equivalent"] / t) if t else 0.0
    return out
