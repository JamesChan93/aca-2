# Dependency locks

`pyproject.toml` is the source of runtime and development requirements. `build.in`
contains the isolated wheel build tools. `runtime.lock`, `development.lock` and
`build.lock` pin transitive dependencies and SHA-256 distribution hashes.

Regenerate with CPython 3.14 and pip-tools 7.6.2:

```sh
pip-compile --allow-unsafe --generate-hashes --strip-extras --no-emit-index-url --no-emit-trusted-host -c requirements/constraints.in --output-file requirements/runtime.lock pyproject.toml
pip-compile --allow-unsafe --generate-hashes --strip-extras --no-emit-index-url --no-emit-trusted-host -c requirements/constraints.in --extra dev --output-file requirements/development.lock pyproject.toml
pip-compile --allow-unsafe --generate-hashes --no-emit-index-url --no-emit-trusted-host -c requirements/constraints.in --output-file requirements/build.lock requirements/build.in
```

Review the diff, release notes, supported Python/wheels, licences and advisories.
Install with `pip install --require-hashes -r requirements/development.lock`, then
`pip install --no-deps --no-build-isolation .`. Validate on Linux amd64 in CI.
Research dependencies are deferred to Phase 3; no empty or unverified research lock
is presented as a tested environment. Native libraries in Psycopg, SQLAlchemy,
Pydantic and development tools are upstream dependencies, not custom services.

The constraints retain the actually tested platformdirs version after an inconsistent
simple-index response, and explicitly lock pip/setuptools. `--allow-unsafe` is pip-tools'
term for including packaging tools; it does not disable hash checking or permit an
unreviewed dependency. See ADR A16 in docs/architecture.md.
