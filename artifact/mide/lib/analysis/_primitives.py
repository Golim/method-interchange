from typing import Dict


def extract_source_content_type_bucket(headers: Dict[str, str]) -> str:
    """Return one of {"json", "form-urlencoded", "multipart", "unknown"}.

    Strips charset; case-insensitive header lookup.
    """
    for k, v in headers.items():
        if k.lower() == "content-type":
            ct = v.lower().split(";", 1)[0].strip()
            if "application/json" in ct:
                return "json"
            if "application/x-www-form-urlencoded" in ct:
                return "form-urlencoded"
            if "multipart/form-data" in ct:
                return "multipart"
    return "unknown"
