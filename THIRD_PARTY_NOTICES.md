# Third-party notices and source provenance

The root MIT license covers original project contributions only. It does not
relicense the third-party reference files listed below or external dependencies.
These reference copies are not compiled into the driver. Original notices and
license terms remain applicable to each copy. Full upstream license texts are
preserved in `LICENSES/`.

## BoxLite reference sources — Apache-2.0

Source: https://github.com/boxlite-ai/boxlite/tree/fc65419699906b41aa5e960a9e8495ade19a5395

- `docs/upstream/boxlite-types.rs.txt`: unchanged copy of
  `src/cli/src/commands/serve/types.rs`.
- `docs/upstream/boxlite-config.rs.txt`: unchanged copy of
  `src/cli/src/commands/serve/handlers/config.rs`.

Applicable license: `LICENSES/BoxLite-Apache-2.0.txt`. Upstream NOTICE is preserved
verbatim in `LICENSES/BoxLite-NOTICE.txt`. BoxLite is mixed-license; the copied
CLI files are outside its separately licensed `apps/` workspace. No `apps/`
source is included here. No modifications were made to these two copies.

## Nomad driver interface reference — MPL-2.0

Source: https://github.com/hashicorp/nomad/blob/9dcbdc5e64ecc4ec63e42b225af729c87c87cf83/plugins/drivers/driver.go

`docs/upstream/nomad-drivers.go.txt` is an unchanged copy of that source file.
Copyright IBM Corp. 2015, 2026. Its SPDX MPL-2.0 notice is preserved. The enclosing
`plugins/LICENSE` applies: `LICENSES/Nomad-plugins-MPL-2.0.txt`. This copy remains
MPL-2.0, including its Source Code Form; it is not relicensed under MIT.

## Nomad dependency manifest reference — BSL-1.1

Source: https://github.com/hashicorp/nomad/blob/9dcbdc5e64ecc4ec63e42b225af729c87c87cf83/go.mod

`docs/upstream/nomad-v2.0.7-go.mod` is an unchanged reference copy. The filename
is historical; the exact source commit above, not the filename, identifies it.
The enclosing repository's BSL-1.1 terms and Additional Use Grant are preserved
in `LICENSES/Nomad-BSL-1.1.txt`. This file is not MIT or MPL-2.0 merely because
the adjacent plugin interface has a different license. The accompanying
`nomad-v2.0.7-ref.json` records this public commit identifier and URL.

## Nomad skeleton dependency manifest reference — MPL-2.0

Source: https://github.com/hashicorp/nomad-skeleton-driver-plugin/blob/689058a3118e4a47a05b44766cf70b6f15989522/go.mod

`docs/upstream/skeleton-go.mod` is an unchanged copy, verified byte for byte
against upstream. Copyright and full license are retained in
`LICENSES/Nomad-skeleton-MPL-2.0.txt` (Copyright IBM Corp. 2019, 2025).

## External dependencies and runtime artifacts

`go.mod`/`go.sum`, guest `package-lock.json` and `uv.lock` identify dependencies
fetched separately under their respective upstream licenses. Dependency source,
compiled driver binaries, container images and BoxLite runtime binaries are not
vendored or released by this source publication. Any later binary or image
release needs its own complete dependency-license inventory and accompanying
notices. No right to third-party trademarks is granted by this project's MIT
license.
