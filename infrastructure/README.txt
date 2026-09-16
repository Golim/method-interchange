PUBLIC INFRASTRUCTURE AND CONTAINER GUIDANCE

Target platform
---------------
The recommended public platform is CloudLab because the artifact needs only one
ordinary Linux node and CloudLab provides a standard Ubuntu profile. CloudLab's
getting-started guide is at https://docs.cloudlab.us/getting-started.html and its
single-pc Ubuntu example is listed at https://www.cloudlab.us/example-profiles.php.

No special hardware is required. The offline claims need 2 CPU cores, 4 GB RAM,
and 10 GB free disk. Installation needs outbound HTTPS access to PyPI and the
Playwright Chromium download; the offline claims need no network. On Ubuntu, the
installer also installs Chromium's system packages and may invoke sudo.

CloudLab procedure
------------------
1. Create or join a CloudLab project and add an SSH public key to your account.
2. Choose Start Experiment, select the facility's `single-pc-ubuntu` profile,
   and select a node with at least 2 cores, 4 GB RAM, and 10 GB free disk.
3. Instantiate the experiment, wait until the node is ready, and SSH to it using
   the command shown by CloudLab's List View.
4. On the node, run:

     git clone https://github.com/Golim/method-interchange.git
     cd method-interchange
     ./install.sh
     ./run-demo.sh

5. For the claim-specific checks, run `./claims/claim1/run.sh`,
   `./claims/claim2/run.sh`, and `./claims/claim3/run.sh`. The generated files are
   under `results/`; the `expected/README.txt` file in each claim describes the
   checks.

CloudLab accounts, projects, node availability, and profile names are controlled by
CloudLab. If `single-pc-ubuntu` is unavailable, use any Ubuntu 22.04+ x86-64 node
with the stated resources and document the selected profile in the AE submission.

Provisioning
------------
Create a standard Ubuntu VM, clone or unpack the artifact, and run:

  ./install.sh

The script creates artifact/mide/.venv from the pinned uv.lock and downloads
Chromium. Installation normally takes several minutes. Then run any claims/claim*/run.sh.

Container alternative
---------------------
The easiest container workflow uses Docker Compose from the repository root:

  docker compose run --rm artifact

This builds `artifact/mide/Dockerfile`, mounts the repository at `/workspace`, and
runs the safe offline demonstration. To run an individual claim:

  docker compose run --rm artifact ./claims/claim2/run.sh

For MIDE's browser runtime without Compose, build the provided container directly:

  docker build -t mide artifact/mide

No reviewer access account, private service, or proprietary component is required.

Other public platforms
-----------------------
The same native workflow works on an Ubuntu VM from Chameleon, FABRIC, or SPHERE.
Use the platform's current account, reservation, SSH, and VM documentation; once
logged into the VM, the commands above are unchanged. Docker is optional and may
be used when the platform provides it, but it is not required for the claims.

Google Colab
------------
Google Colab is also suitable for the safe offline demonstrations. Create a new
notebook, select a standard CPU runtime, and run:

  !git clone https://github.com/Golim/method-interchange.git
  %cd method-interchange
  !UV_CACHE_DIR=/tmp/mide-uv-cache ./install.sh
  !./run-demo.sh
  !./claims/claim1/run.sh
  !./claims/claim2/run.sh
  !./claims/claim3/run.sh

No GPU is required. Installation needs outbound network access, while the claim
workflows make no network requests after installation. Colab runtimes are
temporary; download or copy `results/` before disconnecting. Colab resource
availability and runtime duration are not guaranteed, so record the notebook's
runtime type and the repository commit used for evaluation. See the current
Colab limits at https://research.google.com/colaboratory/faq.html.

Do not run live crawling, CSRF replay, or WCD confirmation in Colab unless the
target owner has explicitly authorized the activity. Those modes are outside the
default artifact evaluation workflow and must also comply with Colab's usage
policies.

Evaluation scope and runtime
----------------------------
`./run-demo.sh` and all three claim runners are offline after installation and
normally finish in under two minutes on the stated VM. Installation generally takes
several minutes because it creates the locked Python environment and downloads
Chromium. Live crawling, CSRF replay, and WCD confirmation are not part of the
default AE workflow; they require explicit authorization and may take substantially
longer depending on the target set.
