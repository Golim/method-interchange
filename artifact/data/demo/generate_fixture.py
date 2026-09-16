#!/usr/bin/env python3
"""Generate the deterministic 30-record synthetic MIDE replay fixture."""

import json
from pathlib import Path


OUT = Path(__file__).with_name("demo.example-replay-records.jsonl")


def response(kind, variant, equivalent=True):
    if kind == "html":
        if equivalent:
            body = f"<html><body><main><h1>Result {variant}</h1><p>synthetic</p></main></body></html>"
        else:
            body = "<html><body><main><form><input></form></main></body></html>"
        return {"status_code": 200 if equivalent else 405, "headers": {"Content-Type": "text/html"}, "body": body}
    if equivalent:
        body = json.dumps({"status": "ok", "result": {"id": variant, "items": ["synthetic"]}})
        return {"status_code": 200, "headers": {"Content-Type": "application/json"}, "body": body}
    return {"status_code": 400, "headers": {"Content-Type": "application/json"}, "body": '{"status":"error","reason":"rejected"}'}


def record(index, baseline_class, content_type, kind, array_style="na", token=False, cache="non_cacheable"):
    path = f"/demo/{content_type.replace('/', '-')}/{baseline_class.lower()}/{index}"
    url = f"https://demo-{(index - 1) // 5 + 1}.invalid{path}"
    headers = {"Content-Type": content_type}
    if content_type == "application/x-www-form-urlencoded":
        body = f"q=item{index}" + ("&csrf_token=synthetic-token" if token else "")
        bucket = "form-urlencoded"
    elif content_type == "application/json":
        payload = {"query": f"item{index}"}
        if token:
            payload["csrf_token"] = "synthetic-token"
        if array_style != "na":
            payload["items"] = ["one", "two"]
        body, bucket = json.dumps(payload), "json"
    else:
        headers["Content-Type"] = "multipart/form-data; boundary=synthetic"
        body, bucket = f"--synthetic\r\nname=field\r\n\r\nitem{index}\r\n--synthetic--", "multipart"

    post = response(kind, index, True)
    converted = response(kind, index, baseline_class in {"A", "B"})
    baseline = response(kind, index, baseline_class in {"B", "C"})
    candidates = []
    styles = [array_style] if array_style != "dual" else ["php", "repeated"]
    for style in styles:
        query = f"q=item{index}"
        if style == "php":
            query += "&items[]=one&items[]=two"
        elif style == "repeated":
            query += "&items=one&items=two"
        if token:
            query += "&csrf_token=synthetic-token"
        candidates.append({
            "array_style_used": style,
            "converted_url": f"{url}?{query}",
            "headers": {},
            "get_response": converted,
            "no_data_get": {"url": url, "response": baseline},
        })
    return {
        "url": url, "method": "POST", "headers": headers, "postData": body,
        "timestamp": f"2026-05-01T00:00:{index:02d}Z", "resourceType": "fetch",
        "response": post, "source_content_type_bucket": bucket, "replay_skipped": None,
        "lossy_conversion": False, "get_candidates": candidates,
        "synthetic_security": {"csrf_scenario": "post_rejects_get_accepts" if token else "none", "wcd_recorded_outcome": cache},
    }


def main():
    classes = ["A"] * 10 + ["B"] * 8 + ["C"] * 6 + ["D"] * 6
    types = (["application/x-www-form-urlencoded", "application/json", "multipart/form-data"] * 10)
    records = []
    for index, baseline_class in enumerate(classes, 1):
        content_type = types[index - 1]
        array_style = "dual" if content_type == "application/json" and index in {2, 5, 8, 11, 14, 17} else "na"
        kind = "html" if index % 2 else "json"
        records.append(record(index, baseline_class, content_type, kind, array_style, token=index in {1, 2, 4, 5, 7, 8}, cache="cacheable" if index in {1, 4, 7} else "non_cacheable"))
    OUT.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in records) + "\n", encoding="utf-8")
    print(f"Wrote {len(records)} synthetic replay records to {OUT}")


if __name__ == "__main__":
    main()
