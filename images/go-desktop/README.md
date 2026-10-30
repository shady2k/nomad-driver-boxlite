# Generic ARM64 Go/desktop worker base

Prepared source recipe only; no image is built or published by this directory. Supply an explicit pinned prepared Prime `BASE_IMAGE`, select Linux ARM64 and build the `go-desktop` target. The recipe adds pinned Go/Zig, desktop development libraries, generic shell tools and `unzip`. It installs bundled official Prime skills into the selected kernel environment and checks acpx runtime import before declaring worker contract 2.

There is no project checkout, source snapshot, node_modules or project module/build cache. Upload a selected worktree or Git bundle and install project dependencies inside the guest. Bun is not baked in; `unzip` supports official installers. Existing published images and active sessions are unchanged. Matching OCI labels declare compatibility; guest readiness and actual build/test evidence remain required.
