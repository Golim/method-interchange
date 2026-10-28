"""Deterministic local target for exercising MIDE's crawl/replay pipeline."""

from flask import Flask, jsonify, request

app = Flask(__name__)

CASES = {
    "echo": ("URL-encoded: POST and converted GET should match", ""),
    "post-only": ("POST-only: converted GET should be rejected", ""),
    "different": ("Different response: GET is accepted but differs", ""),
    "multipart": ("Multipart: POST and converted GET should match", ' enctype="multipart/form-data"'),
    "json": ("JSON fetch: POST and converted GET should match", " data-json"),
    "required": ("Parameter-dependent: converted value is required", ""),
}


@app.get("/")
def index():
    links = "\n".join(
        f'<li><a href="/case/{name}">{title}</a></li>'
        for name, (title, _) in CASES.items()
    )
    return f"""<!doctype html>
<html><head><title>MIDE local fixture</title></head>
<body><h1>MIDE local test target</h1><p>Choose a case:</p><ul>{links}</ul></body></html>"""


@app.get("/case/<name>")
def case_page(name):
    if name not in CASES:
        return "Unknown case", 404
    title, option = CASES[name]
    action = f"/endpoint/{name}"
    json_attr = " onsubmit=\"return sendJson(event)\"" if option == " data-json" else ""
    enctype = option if option.startswith(" enctype=") else ""
    script = """<script>
async function sendJson(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const value = form.get('value') || '';
  await fetch(event.target.action, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value})
  });
  return false;
}
</script>""" if json_attr else ""
    return f"""<!doctype html>
<html><head><title>{title}</title></head><body>
<h1>{title}</h1><form method="post" action="{action}"{enctype}{json_attr}>
<label>Value <input name="value" type="text"></label>
<button type="submit">Send</button></form>{script}</body></html>"""


def submitted_value():
    if request.is_json:
        return str((request.get_json(silent=True) or {}).get("value", ""))
    return str(request.form.get("value", ""))


@app.route("/endpoint/echo", methods=["GET", "POST"])
def echo():
    value = request.args.get("value", "") if request.method == "GET" else submitted_value()
    return jsonify(value=value)


@app.post("/endpoint/post-only")
def post_only():
    return jsonify(value=submitted_value())


@app.get("/endpoint/post-only")
def post_only_get():
    return "GET is not allowed for this endpoint", 405


@app.route("/endpoint/different", methods=["GET", "POST"])
def different():
    value = request.args.get("value", "") if request.method == "GET" else submitted_value()
    # `result` is an application outcome field understood by MIDE's comparator.
    # A plain `method` field is treated as potentially dynamic JSON data, so
    # differing only in that value would still be structurally equivalent.
    result = "GET result" if request.method == "GET" else "POST result"
    return jsonify(result=result, value=value)


@app.route("/endpoint/multipart", methods=["GET", "POST"])
def multipart():
    value = request.args.get("value", "") if request.method == "GET" else submitted_value()
    return jsonify(value=value)


@app.route("/endpoint/json", methods=["GET", "POST"])
def json_endpoint():
    value = request.args.get("value", "") if request.method == "GET" else submitted_value()
    return jsonify(value=value)


@app.route("/endpoint/required", methods=["GET", "POST"])
def required():
    if request.method == "GET" and "value" not in request.args:
        return "Converted value is required", 400
    value = request.args.get("value", "") if request.method == "GET" else submitted_value()
    return jsonify(value=value)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
