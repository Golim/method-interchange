from typing import Dict, List

from lib.comparator import ResponseComparator


def _scores_dict(cmp) -> Dict:
    return {
        "exact": cmp.exact,
        "sequence_matcher": cmp.sequence_matcher,
        "structural": cmp.structural,
        "cosine": cmp.cosine,
        "headers_similarity": cmp.headers_similarity,
        "size_ratio": cmp.size_ratio,
        "semantic_mismatch": cmp.semantic_mismatch,
    }


def evaluate_record(record: Dict, comparator: ResponseComparator) -> Dict:
    """evaluate original POSTs against their GET variants.

    Returns:
        {
            "verdict": "equivalent" | "not_equivalent" | "n/a",
            "per_candidate": [
                {
                    "array_style_used": str,
                    "scores": {exact, sequence_matcher, structural,
                               cosine, headers_similarity, size_ratio} | None,
                    "equivalent": bool,
                    "skipped_reason": None | "failure",
                },
                ...
            ],
        }
    """
    if record.get("replay_skipped"):
        return {"verdict": "n/a", "per_candidate": []}

    post_response = record["response"]
    per_candidate = []
    any_equiv = False
    for cand in record["get_candidates"]:
        get_resp = cand["get_response"]
        if get_resp.get("failure") is not None:
            per_candidate.append({
                "array_style_used": cand["array_style_used"],
                "scores": None,
                "equivalent": False,
                "skipped_reason": "failure",
            })
            continue
        cmp = comparator.compare(post_response, get_resp)
        equiv = cmp.is_interchangeable
        per_candidate.append({
            "array_style_used": cand["array_style_used"],
            "scores": _scores_dict(cmp),
            "equivalent": equiv,
            "skipped_reason": None,
        })
        any_equiv = any_equiv or equiv

    return {
        "verdict": "equivalent" if any_equiv else "not_equivalent",
        "per_candidate": per_candidate,
    }


def aggregate(per_record_results: List[Dict]) -> Dict:
    """Return {"equivalent": int, "not_equivalent": int}.

    n/a (replay_skipped) records are NOT counted in either bucket — n/a is its own state.
    """
    equivalent = sum(1 for r in per_record_results if r["verdict"] == "equivalent")
    not_equivalent = sum(1 for r in per_record_results if r["verdict"] == "not_equivalent")
    return {"equivalent": equivalent, "not_equivalent": not_equivalent}


def evaluate_no_data_record(record: Dict, comparator: ResponseComparator) -> Dict:
    """Evaluate POST response against GET controls with converted data removed.

    Returns the same verdict shape as evaluate_record, but each candidate uses
    candidate.no_data_get.response instead of candidate.get_response.
    """
    if record.get("replay_skipped"):
        return {"verdict": "n/a", "per_candidate": []}

    post_response = record["response"]
    per_candidate = []
    any_equiv = False
    saw_candidate = False

    for cand in record.get("get_candidates", []):
        no_data_get = cand.get("no_data_get")
        if not isinstance(no_data_get, dict):
            per_candidate.append({
                "array_style_used": cand.get("array_style_used"),
                "url": None,
                "scores": None,
                "equivalent": False,
                "skipped_reason": "missing_no_data_get",
            })
            continue

        saw_candidate = True
        no_data_resp = no_data_get.get("response", {})
        if no_data_resp.get("failure") is not None:
            per_candidate.append({
                "array_style_used": cand.get("array_style_used"),
                "url": no_data_get.get("url"),
                "scores": None,
                "equivalent": False,
                "skipped_reason": "failure",
            })
            continue

        cmp = comparator.compare(post_response, no_data_resp)
        equiv = cmp.is_interchangeable
        per_candidate.append({
            "array_style_used": cand.get("array_style_used"),
            "url": no_data_get.get("url"),
            "scores": _scores_dict(cmp),
            "equivalent": equiv,
            "skipped_reason": None,
        })
        any_equiv = any_equiv or equiv

    if not saw_candidate:
        return {"verdict": "n/a", "per_candidate": per_candidate}
    return {
        "verdict": "equivalent" if any_equiv else "not_equivalent",
        "per_candidate": per_candidate,
    }


def aggregate_no_data(per_record_results: List[Dict]) -> Dict:
    """Return equivalent/not_equivalent/n/a counts for no-data GET controls."""
    equivalent = sum(1 for r in per_record_results if r["verdict"] == "equivalent")
    not_equivalent = sum(1 for r in per_record_results if r["verdict"] == "not_equivalent")
    not_available = sum(1 for r in per_record_results if r["verdict"] == "n/a")
    return {
        "equivalent": equivalent,
        "not_equivalent": not_equivalent,
        "not_available": not_available,
    }
