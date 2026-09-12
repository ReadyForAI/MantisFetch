# Deployment notes

Operational guidance for running MantisFetch in production. See the README for
the full environment-variable reference.

## Access control (recap)

`/web`, `/doc` and `/mcp` are **loopback-only by default**. Set
`MANTISFETCH_MCP_TOKEN` and send `Authorization: Bearer <token>` to reach them
from another host. Note that a request to the published port of a *container*
arrives from the Docker bridge — a non-loopback peer — so it is denied (403)
without a token even from the same machine. `/health` is always exempt.

**Off-host means TLS.** The token is a bearer credential: over plain http it
travels in the clear on every request, and anything on the path can replay it
against a surface that drives a browser and reads files. Set
`MANTISFETCH_TLS_CERTFILE` and `MANTISFETCH_TLS_KEYFILE` (both, or neither
takes effect) so the listener serves https, or keep the hop inside a tunnel or
an mTLS mesh. Loopback and same-host container traffic are not affected.

The same token gates `/mcp` and the REST surface, but the two gates decide in a
different order, which is worth knowing when a call is refused:

| | token set | token unset |
|---|---|---|
| `/mcp` | every peer must present it, **loopback included** | loopback allowed; other peers 403 |
| `/web` `/doc` `/deliverables` | **loopback allowed without it**; other peers must present it | loopback allowed; other peers 403 |

So a same-host client configured with the *wrong* token sees REST succeed and
MCP answer 401. Send the bearer on every call — it is the only configuration
that is correct on both faces.

## Two provider slots, and when the second one is used

With `MANTISFETCH_LLM_DEFAULT` / `MANTISFETCH_LLM_EXTRA` configured, a failure
on the primary falls over to the secondary when — and only when — the other
vendor might plausibly do better:

| failure | retried here | falls over |
|---|---|---|
| 429, 5xx, timeout, connection | yes | yes |
| **401 / 403 / 404** — expired key, revoked permission, retired model | no | **yes** |
| 400 / 422 / content policy | no | no |

The middle row is the reason to configure a second slot at all: none of those
say anything about whether the peer can serve the call, and retrying the same
vendor is pointless. A malformed request is different — the peer would reject
it too, and failing it over only spends a second vendor's quota.

Single-slot deployments are unaffected: with nothing to fall over to, all three
rows end the same way, with the error recorded on the document.

## Summarisation is process-wide

`MANTISFETCH_DEFERRED_SUMMARY_MAX_CONCURRENT` (default 1) and
`MANTISFETCH_DEFERRED_SUMMARY_MAX_QUEUED` (default 64) bound **the whole
process**, not one caller. One MantisFetch typically serves several NodalOS
instances and every agent on them, and every MCP ingest defers its summary — the
tool always declares a budget, and a declared budget defers. So arrivals are a
fan-in while the drain is this process making one section-by-section pass at a
time.

Past the queue bound a document is stored with `summary_status: not_queued`:
the extraction is on disk and readable, only the summary did not happen, and
`POST /doc/library/{doc_id}/summary` retries it. That is deliberately louder
than an unbounded queue, where a caller reads `pending` from a document nobody
is working on.

Raising `MAX_CONCURRENT` raises the drain rate and the parallel load on the LLM
provider together; pick it against that provider's rate limit. A summary costs
one call per section plus two, so a 100-section document is ~102 calls.

A restart cannot carry deferred summaries with it (they live in daemon
threads). On startup the status face is corrected so it stops claiming work
that no longer exists, and where it lands depends on how that kind of document
is retried:

- **Uploads:** `running` goes back to `pending` (and `pending` stays). Retry with
  `POST /doc/library/{doc_id}/summary`, which accepts `pending`.
- **Web captures:** `running` *and* `pending` become `failed` with
  `error_code: summary_interrupted`. The summary endpoint does not take web
  captures; capturing the same page again with `summary_mode: "defer"` hits the
  cache and schedules a new summary — and it only does that for a status other
  than pending/running/completed, which is why a capture is not left at
  `pending`.

Nothing is re-queued automatically, because with fan-in that would refill the
queue at the worst moment. Retry the ones you care about.

## Request size

`/web` and `/doc` stop reading a request body once it passes
`MANTISFETCH_MAX_UPLOAD_MB + 1 MiB` and answer `413`. The slack is the multipart
envelope around a file that is itself at the limit; there is no separate key,
so raising the upload limit raises this with it.

This is the outer bound. The per-file limits still run inside the handlers —
`MANTISFETCH_MAX_UPLOAD_MB` for the parse channel and
`MANTISFETCH_RAW_MAX_{MD,IMAGE}_MB` for `store_only` — because both need the
filename, which is only known once the form has been parsed. So a 100 MiB `.md`
is read up to the outer ceiling before its 2 MiB per-type refusal; a 300 MiB one
is cut off at the ceiling.

`/mcp` is not behind this. It has its own body limit derived from the inline
document cap, and its transport reads the body itself.

### Upload admission (`429` on `/doc/parse`)

Before any of a `/doc/parse` body is read, the request is admitted by its
`Content-Length` against `MANTISFETCH_PARSE_QUEUE_MAX_BYTES`, which counts
uploads still arriving together with uploads already queued for parse. A
request that does not fit gets `429` with `Retry-After: 30`, and none of it is
read or stored. A request that declares no length is counted as the outer
ceiling above. Once the handler has its own copy of the file, the reservation
is handed over to the parse queue's count of staged bytes rather than freed:
those bytes stay counted until the parse finishes.

This runs in the `/doc` service itself, so MCP ingests pass through it too, even
though they skip the outer ceiling.

So one number bounds what a burst of uploads can put on disk: the system temp
dir, where the multipart body is spooled, plus `.upload-tmp` under the library,
where queued uploads wait. A `429` here normally means the service is full, not
that the file is bad, so retry it. If it shows up under normal load, the budget
is too small for the fan-in, and raising it means more disk at both places.

The budget must also hold at least one request of the largest size: a request
bigger than the whole budget is refused even when nothing else is in flight,
and retrying will not help. That includes any request without a declared length
once the budget is set below the outer ceiling. The default is ten times the
upload cap.

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
