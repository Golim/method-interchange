Expected validation
-------------------
Run with `docker compose run --rm artifact ./claims/claim4/run.sh`. The runner
checks that MIDE captures all six fixture endpoints, emits replay records, and
writes an analysis file. It checks that echo, multipart, JSON, and parameter-
dependent GETs are accepted and equivalent; the POST-only endpoint rejects GET;
and the different-response endpoint accepts GET but is not equivalent.

The pipeline reaches the fixture over the Compose network. The fixture is also
available at `http://127.0.0.1:5080` on the Docker host for inspection. After the
run, stop its service with `docker compose down`.
