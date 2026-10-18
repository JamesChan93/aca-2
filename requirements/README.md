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
`research.lock` adds only NumPy 2.5.4 for statistics. Install both development and
research locks to run the complete suite. The production runtime image does not
install NumPy; statistical work runs locally. Pandas, SciPy, PyArrow and DuckDB
remain deferred under ADR A24 because this bounded CSV workflow does not need them. Native libraries in Psycopg, SQLAlchemy,
Pydantic and development tools are upstream dependencies, not custom services.

The constraints retain the actually tested platformdirs version after an inconsistent
simple-index response, and explicitly lock pip/setuptools. `--allow-unsafe` is pip-tools'
term for including packaging tools; it does not disable hash checking or permit an
unreviewed dependency. See ADR A16 in docs/architecture.md.

Regenerate the optional research lock with:

```sh
pip-compile --allow-unsafe --generate-hashes --strip-extras --no-emit-index-url --no-emit-trusted-host -c requirements/constraints.in --extra research --output-file requirements/research.lock pyproject.toml
```
