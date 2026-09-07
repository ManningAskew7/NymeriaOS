# NymeriaOS Backend

NymeriaOS is a personal AI assistant backend built on LangGraph, FastAPI, and
local-first persistence.

See [docs/getting-started/architecture.md](docs/getting-started/architecture.md)
for the architecture overview and
[docs/getting-started/QUICKSTART.md](docs/getting-started/QUICKSTART.md)
for local setup.

## Install

Two channels, both giving you the `nymeria` command:

```bash
uv tool install nymeriaos
# update later: uv tool upgrade nymeriaos
```

```bash
git clone https://github.com/ManningAskew7/NymeriaOS.git ~/NymeriaOS
uv tool install --editable ~/NymeriaOS/Nymeria
# update later: git -C ~/NymeriaOS pull --ff-only
```

Either Docker shape builds its images from the source checkout; the beta
publishes no container images.
