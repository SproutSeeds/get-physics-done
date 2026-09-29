# About this fork

This repository is a community-maintained fork of
[Get Physics Done (GPD)](https://github.com/psi-oss/get-physics-done), the
open-source agentic AI system for physics research created by
[Physical Superintelligence PBC (PSI)](https://www.psi.inc).

Upstream has not merged a change since May 28, 2026. On September 29, 2026 it
had 36 open pull requests and 7 open issues waiting for review. This fork keeps
GPD working, reviews issues and pull requests, and ships fixes. It is maintained
by [SproutSeeds](https://github.com/SproutSeeds) and is not affiliated with or
endorsed by PSI.

## Install the maintained version

```bash
npx -y github:SproutSeeds/get-physics-done --upgrade
```

`--upgrade` installs the Python package from this fork's `main` branch into
`${GPD_HOME:-~/.gpd}/venv`. A plain `npx -y get-physics-done`, or a later
`--reinstall`, installs PSI's last PyPI release (1.2.2) instead, which does not
include the fixes below.

### Optional: your own OpenAlex key

GPD searches OpenAlex before falling back to arXiv. OpenAlex now meters
requests: without a key, everyone on your network's IP address shares a free
budget of $0.10 a day, about 100 searches, and once it runs out GPD uses
arXiv's own API for the rest of the day. A free personal key has its own budget
([how to get one](https://help.openalex.org/api/authentication/)). Set it
before starting your runtime:

```bash
export OPENALEX_API_KEY=your-key
```

On macOS you can keep the key in the Keychain instead, which also reaches
runtimes that do not pass environment variables to MCP servers. When
`OPENALEX_API_KEY` is unset, GPD reads the Keychain item with service
`get-physics-done` and account `OPENALEX_API_KEY` (or the item that
`orp secrets keychain-add --alias openalex-api-key --provider openalex`
creates):

```bash
security add-generic-password -s get-physics-done -a OPENALEX_API_KEY -w
```

To work from a source checkout:

```bash
git clone https://github.com/SproutSeeds/get-physics-done
cd get-physics-done
uv sync --dev
uv run gpd --help
```

## Changes from upstream

| Change | Why | Upstream reference |
| --- | --- | --- |
| Bound `mcp` below 2 | mcp 2.0 (July 28, 2026) removed `mcp.server.fastmcp`, so seven of the nine built-in MCP servers failed to import on fresh installs | [#273](https://github.com/psi-oss/get-physics-done/issues/273), [#276](https://github.com/psi-oss/get-physics-done/pull/276) |
| Look up arXiv papers in OpenAlex by landing page | OpenAlex no longer resolves arXiv DOIs (`10.48550/arxiv.*`) as work DOIs, so arXiv abstract lookups returned HTTP 404 (seen September 29, 2026) | affects upstream too; fork only for now |
| Paper search finds the right papers | Search matched plain multi-word queries loosely, kept only works whose *primary* location is arXiv, and sent arXiv field syntax (`ti:`, `au:`, `abs:`, `ANDNOT`) to OpenAlex, which cannot read it. For "neural network field theory" it returned none of 14 known NNFT papers. Now plain queries search the exact phrase first, any arXiv location counts, arXiv syntax goes to arXiv, thin results are topped up from arXiv, and the arXiv fallback also tries the phrase first: 8 of the top 10 are NNFT papers | affects upstream too |
| OpenAlex API key and budget handling | OpenAlex meters anonymous requests per IP address; `OPENALEX_API_KEY` or a macOS Keychain item sends your own key, and a spent budget (HTTP 429) falls back to arXiv without further OpenAlex calls | fork only |
| Skip ar5iv failed-conversion pages | ar5iv answers some papers (for example hep-th/9711200) with an error page, which was served and cached as the paper; now the PDF is used, and cached error pages are refetched | affects upstream too |
| Requests identify this fork | User agents name this repository instead of PSI's operations contact | fork only |
| Repository and issue links point at this fork | Lets `npx -y github:SproutSeeds/get-physics-done --upgrade` install this fork's `main` | fork only |
| Fork documentation and ownership | This file, the README note, `CODEOWNERS` and the contributing note | fork only |

Apart from these changes the code matches upstream at
[`0f417695`](https://github.com/psi-oss/get-physics-done/commit/0f417695ccf1f987af44bb4f4b3cbbdeb5f2c48b).
PSI's code remains under its Apache 2.0 license and copyright notice, and
changes made here are released under the same license.

## Contributing

Issues, discussions and pull requests are welcome here.

* When you open a pull request, check that the base repository is
  `SproutSeeds/get-physics-done`. GitHub proposes PSI's repository by default
  for branches that live in other forks of the original.
* Contributions to this fork are accepted under the Apache 2.0 license
  (section 5). PSI's contributor license agreement applies only to pull
  requests sent to `psi-oss/get-physics-done`.
* The `human authors` check in CI rejects AI and bot identities in commit
  authors and co-author trailers, as it does upstream.
* See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and test
  commands.

## Relationship to upstream

Fixes that belong upstream are also offered to PSI as pull requests. If PSI
resumes merging, this fork will merge their `main` and keep the table above
current.
