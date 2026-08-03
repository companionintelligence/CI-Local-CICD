"""Local CI/CD tooling for the Companion Intelligence workspace.

Two jobs:

* ``local_ci.workflows`` / ``local_ci.policy`` — gate GitHub Actions workflows
  down to manual (``workflow_dispatch``) so they stop burning billed minutes.
* ``local_ci.detect`` / ``local_ci.runner`` — run the equivalent lint, test and
  build steps on local hardware instead.

COPYRIGHT COMPANION INTELLIGENCE - LIFESCOPE INC. Private and confidential.
"""

__version__ = "0.1.0"
