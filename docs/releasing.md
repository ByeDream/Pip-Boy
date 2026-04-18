# Releasing Pip-Boy

This document describes the version management and release process for the `pip-boy` package.

## Version Management

### Where the version lives

The version number is defined in a single place:

- **`pyproject.toml`** — the `version` field under `[project]`

At runtime, the version is read via `importlib.metadata`:

```python
from pip_agent import __version__
```

There is no separate version file. The `pyproject.toml` version is the single source of truth.

### Semantic Versioning

The project follows [Semantic Versioning](https://semver.org/) (MAJOR.MINOR.PATCH):

- **MAJOR** — Breaking changes to the agent's tool interface, config format, or scaffold structure
- **MINOR** — New features (commands, tools, channels, skills) that are backward-compatible
- **PATCH** — Bug fixes, dependency updates, documentation improvements

## Release Checklist

Follow these steps to release a new version. An AI agent can execute these steps by following this guide.

### 1. Ensure tests pass

```bash
cd Pip-Boy
pytest
```

All tests must pass before proceeding.

### 2. Update the version number

Edit `pyproject.toml` and change the `version` field:

```toml
[project]
version = "X.Y.Z"
```

### 3. Commit the release

```bash
git add pyproject.toml
git commit -m "Release vX.Y.Z"
```

### 4. Tag the release

```bash
git tag vX.Y.Z
```

### 5. Push to GitHub

```bash
git push && git push --tags
```

### 6. Verify CI

The `v*` tag push triggers the **Publish** workflow, which includes its own test step:

- **Publish** (`publish.yml`) — Runs tests, builds the package, and publishes it to PyPI via Trusted Publisher

Note: `ci.yml` only runs on pushes and pull requests to `main`, not on tag pushes. The publish workflow has its own test job to ensure correctness before publishing.

Check the [Actions tab](https://github.com/ByeDream/Pip-Boy/actions) to verify the publish workflow succeeds.

### 7. Verify on PyPI

After the publish workflow completes, verify the package is available:

```bash
pip install pip-boy==X.Y.Z
```

Or check https://pypi.org/project/pip-boy/

### 8. Update consumers

In any project that consumes `pip-boy` (e.g., `pip-playground`):

```bash
pip install --upgrade pip-boy
```

Or from within a running Pip-Boy session:

```
/update
```

## Scaffold Migration

When releasing a version that changes scaffold template files (`src/pip_agent/scaffold/`), the migration is handled automatically:

- **New scaffold files** — Automatically deployed on next startup
- **Modified templates** — If the user hasn't edited the local copy, it is auto-updated. If locally modified, a warning is printed and the file is left untouched
- **Removed templates** — A warning is printed suggesting manual cleanup. Files are not auto-deleted

The scaffold manifest (`.pip/.scaffold_manifest.json`) tracks file hashes and the version that installed each file.

## Troubleshooting

### CI publish fails

- Verify the tag format matches `v*` (e.g., `v0.2.0`, not `0.2.0`)
- Ensure the PyPI Trusted Publisher is configured for the GitHub repository
  (Settings > Publishing > Add a new publisher: GitHub Actions, repo `ByeDream/Pip-Boy`, workflow `publish.yml`, environment `pypi`)

### Version conflict on PyPI

PyPI does not allow overwriting an already-published version. If you need to fix a released version:

1. Increment the PATCH version (e.g., `0.2.0` -> `0.2.1`)
2. Follow the full release checklist above

### Package not found after publish

- The PyPI index may take a few minutes to update
- Try: `pip install --no-cache-dir pip-boy==X.Y.Z`

## First-Time PyPI Setup

Before the first release, configure PyPI Trusted Publisher:

1. Go to https://pypi.org and create an account (if needed)
2. Go to https://pypi.org/manage/account/publishing/
3. Add a new pending publisher:
   - PyPI project name: `pip-boy`
   - Owner: `ByeDream`
   - Repository: `Pip-Boy`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
4. In the GitHub repo, create an environment named `pypi`:
   - Settings > Environments > New environment > Name: `pypi`
   - No additional protection rules needed (the tag trigger is sufficient)
