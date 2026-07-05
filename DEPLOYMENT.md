# Deployment notes

Operational guidance for running MantisFetch in production. See the README for
the full environment-variable reference.

## Access control (recap)

`/web`, `/doc` and `/mcp` are **loopback-only by default**. Set
`MANTISFETCH_MCP_TOKEN` and send `Authorization: Bearer <token>` to reach them
from another host. Note that a request to the published port of a *container*
arrives from the Docker bridge — a non-loopback peer — so it is denied (403)
without a token even from the same machine. `/health` is always exempt.

## Container hardening

`docker-compose.yml` runs the service with:

```yaml
    cap_drop: ["ALL"]
    cap_add: ["DAC_OVERRIDE"]
    security_opt: ["no-new-privileges:true"]
```

This is safe for MantisFetch specifically:

- The app binds port **9898** (> 1024), so it does not need `NET_BIND_SERVICE`.
- Chromium is launched with **`--no-sandbox`**, so it needs neither the setuid
  sandbox helper (`no-new-privileges` would block it) nor `SYS_ADMIN` for a
  user-namespace sandbox (`cap_drop: ALL` would block it).
- It writes only paths it owns (the docs library, the OCR cache, and a `/tmp`
  scratch dir for page rendering).

`cap_drop: ALL` drops the ~13 default capabilities the app never uses
(`NET_RAW`, `MKNOD`, `SETUID`/`SETGID`, `SYS_CHROOT`, `SETFCAP`, …), which is the
bulk of the escalation surface. **`DAC_OVERRIDE` is added back** for one reason:
the default bind mount `${HOME}/.mantisfetch/docs` may already exist **owned by a
different host user** (e.g. from a prior `python mantisfetch_server.py` run), and
without `DAC_OVERRIDE` the container's root could not write it — a working
`docker compose up` would silently fail to persist captures/parses.

**Maximal hardening:** if you align the docs volume so the container's user/group
owns it (root-owned by default, or the shared-group setup below), you can drop
`DAC_OVERRIDE` too — then the container's root can no longer override file
permissions at all. In that case the volume **must** be writable on its own
permissions, not because "root can write anything".

Smoke-test after enabling: capture a page (`/web/capture`), parse a scanned PDF
(`/doc/parse`), and — if enabled — exercise local OCR.

> `read_only: true` (a read-only root filesystem) is a further step, not enabled
> by default: it requires declaring every writable path (the docs volume, the
> `/tmp` OCR scratch, and Chromium's cache) as a `tmpfs`/volume, so it needs
> per-deployment testing.

## Shared document library (volume ownership)

The document library is a bind-mounted volume (`…:/root/.mantisfetch/docs` by
default). When the same directory is exported over SMB/NFS to a **separate
account** (e.g. `smbuser`), align ownership with a **shared group + setgid**
rather than a shared UID — the app already supports this
(`mantisfetch_common/atomic.py` gives each written file the destination
directory's group read/write bits).

```bash
# Host-side, once. Pick a gid; add every consumer (smbuser, the deploy user…) to it.
groupadd -g 1500 mantis
usermod -aG mantis smbuser
chown -R root:mantis /srv/mantisfetch/docs
chmod -R 2775 /srv/mantisfetch/docs      # leading 2 = setgid
```

Now files the container writes (as root) inherit the `mantis` group via setgid
and are group-writable, so `smbuser` — a member of `mantis` — can read and write
them. This works with `cap_drop: ALL` because root writes files it **owns** (no
permission override needed).

If consumers reach the library **only over SMB** (never reading the volume
directly), the simpler alternative is Samba's `force user`/`force group`, which
maps all SMB clients to `smbuser` regardless of on-disk ownership.

### Running the process as non-root (optional)

Some hardened environments require a non-root PID 1. Do this as a **runtime
override**, not baked into the image, so the uid/gid matches the shared group:

```yaml
    user: "1500:1500"
    group_add: ["1500"]
```

The mounted docs path must then be writable by that uid/gid, and moved out of
`/root/` (e.g. mount at `/data` and set `MANTISFETCH_DOCS_DIR=/data`).

## Single-process boundary

The doc-id counters, the doc-index lock, and the per-document/per-capture locks
are all **in-process** (`threading`/`asyncio`). Run **one MantisFetch process per
document library** — do not point multiple containers or `uvicorn --workers > 1`
at the same volume, or you can get duplicate ids and lost doc-index updates.
Scale by giving each instance its own library, not by sharing one.
