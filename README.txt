ACSAC ARTIFACT: When POST Becomes GET

This repository contains the MIDE source code, offline evaluation scripts, and a
small synthetic JSONL fixture. The fixture exercises the artifact safely without
network access; it does not reproduce the paper's full Internet-scale counts.

Quick start
-----------
1. On Ubuntu 22.04+ x86-64 with Python 3.11+, run: ./install.sh
2. Run all safe demonstrations: ./run-demo.sh
3. Or run a single paper claim: ./claims/claim1/run.sh

Docker Compose
--------------------------
With Docker and the Docker Compose plugin installed, run the complete offline demo:

  docker compose run --rm artifact

Run an individual claim by replacing the default command:

  docker compose run --rm artifact ./claims/claim1/run.sh

The Compose service mounts the repository only to write generated `results/` files.
All bundled demos remain offline after the image is built.

The generated results are placed in results/ and can be compared with each claim's
expected/ directory. See infrastructure/README.txt for resource requirements,
claims/ for result-specific instructions, use.txt for safety constraints, and
license.txt for license information.
