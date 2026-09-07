# Named storage spaces

Named spaces are an explicit local administration boundary. Each points to a
separate existing Brain database and asset directory. No operation searches other
spaces to resolve an ID. The default legacy `--root` interface remains available.

```sh
# Register an existing corpus as personal; this does not ingest or migrate it.
research --registry /absolute/registry spaces register personal \
  --owner owner --kind personal --data-root /absolute/existing-brain

# Create an empty project; an existing target directory is refused.
research --registry /absolute/registry spaces create lab \
  --owner owner --kind shared --data-root /absolute/new-lab

research --registry /absolute/registry --space personal search "activation steering"
research --registry /absolute/registry spaces list
research --registry /absolute/registry spaces membership lab colleague --role member
research --registry /absolute/registry spaces membership lab colleague --role revoke
```

`RESEARCH_SPACE_REGISTRY` sets the default registry directory; otherwise it is
`~/.local/share/research-brain`. CLI output is JSON. `--root` and `--space` are
mutually exclusive. Registering an existing database checks compatibility and
never initializes or migrates it. Roots cannot overlap. Existing corpus ownership
is never inferred or changed automatically.

## Python integration contract

`research_brain.spaces.SpaceRegistry` exposes local administrative registration,
membership, scope creation, validation, and scope-checked reads. An immutable
`ContextScope` contains principal, audience, conversation, writable space,
explicit read spaces, and policy version. Its digest can key scoped caches.

Shared scopes cannot read personal spaces. Private scopes may explicitly attach
authorized shared spaces. Every membership or registry change invalidates all
older scopes conservatively; callers must create a new session, not reuse the old
Pi history with a new scope. `read` checks policy both before retrieval and before
returning. It returns an explicit space identity and never permits model calls.

This is **not yet a Discord security perimeter**: the transport must authenticate
the principal, bind the scope to a session, serialize revocation with delivery,
sanitize output, and constrain tool arguments. Do not expose administrative
`open`, `get`, or `list` directly over a network. Local root paths are trusted
administration. Backend administrators and model providers remain outside the
application privacy guarantee. This foundation does not implement absorption
jobs, publication, a Discord bot, or Pi lifecycle management.
