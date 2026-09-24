"""eval-harvest's packaged Lambda MicroVMs Harbor environment.

A real, installed package so an unmodified Harbor can load
``harvest_env.lambda_microvms:LambdaMicrovmsEnvironment`` by import path (H-3's no-fork design). This
avoids two approaches that cannot load the environment: a ``/tmp`` shim, and a ``PYTHONPATH`` overlay
of Harbor's own source tree, which can never work because the ``harbor.*`` namespace belongs to the
installed distribution and a regular package cannot be partially overlaid by a same-named namespace
portion.

``lambda_microvms`` here is the project's own environment (MIT-0), loaded into stock
``harbor==0.22.0`` by import path. No Harbor source is vendored and the change was not upstreamed as
a stored patch; a Harbor PR, if ever made, is a ``git diff`` generated at that time.
``src/eval_harvest/`` never imports this package — Harbor loads it, from an import-path string
(CLAUDE.md).
"""
