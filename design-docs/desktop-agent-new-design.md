# Desktop agent connectivity: WebSocket relay with agent-sent bootstrap (new design)

**Status:** DOCUMENTED. **Build:** NOT AUTHORIZED.

**Document history:** This is the single governing document for the new
design. It merges two earlier files, now kept as history in
`design-docs/history/`:

- `relay-connectivity.md` (LOCKED, owner decision 2026-10-25) — the
  original relay design (persistent WebSocket, MCP server on the VM,
  mutual TLS with ephemeral Diffie-Hellman).
- `relay-connectivity-bootstrap.md` (owner discussion 2026-10-25
  ~16:09–16:26 PDT) — the revised simplified model: WebSocket relay,
  MCP-free, message-level public-key encryption, and the desktop
  agent itself sending `agent.md` plus the VM-side tools in a
  plaintext bootstrap.

The bootstrap revision supersedes the locked doc on VM-side tooling,
crypto shape, and bootstrap/discovery. Everything else from the
locked doc that is not contradicted here carries over. Where the two
conflict on those three points, this document follows the bootstrap
revision, because it is later and owner-directed.

**Build status, unchanged:** owner directive 2026-10-25 ~16:26 PDT —
document only, do not build now. Nothing here authorizes code,
deployment, installation, or contact with the Mac mini. Build waits
for a later, separate owner go-ahead.

---

## 1. Problem

Tailscale tunnel setup is painful on both sides: install plus setup,
proxy and MagicDNS details leaking into every step. The owner wants
the same outcome with less pain: the VM brain drives the Mac agent,
with no VPN app and no network setup on either side.

Three further problems shaped this design:

- **No repo sharing.** The service code cannot be shared as a repo.
  Any design that requires another person to clone a repo, or to
  discover a separate MCP-server install, fails the shipping test.
- **User-facing connectors are URL plus auth.** In the Muse app,
  adding a connector is Connect plus authenticate, nothing else
  (Settings > Connectors, as screenshotted 2026-10-25). There is
  no install step visible to the user, and no Settings field for
  typing a server URL. A design that needs the user to install a
  server on the VM first does not fit that product model.
- **Discovery.** A fresh VM with no tools and no keys cannot be
  expected to already know a documentation URL (llms.txt or
  similar). Discovery must work in-band, from the agent itself,
  on first contact.

Hard constraints:

- The VM cannot do direct connection: no UDP, no inbound
  connections (verified). The Mac sits behind home NAT with no
  port forward. A relay is therefore required. The relay is
  always in the path; persistence only removes per-message
  setup cost.
- Invocation must be immediate: no polling, no queue. Both sides
  hold persistent connections; a request goes out over an open
  socket and the response comes back over it.
- The relay must be blind to secrets: it must never see tool
  requests, tool results, passwords, private keys, or session
  keys. The one deliberate exception is public bootstrap
  material (agent.md, tools code, public keys) — see §7.
- The owner must hold a kill switch: one action that stops all
  VM access immediately, without touching keys or the Mac.

---

## 2. Goals

- Zero network configuration on either side: no VPN app, no
  port forward, no MagicDNS, no proxy settings.
- One session, VM to agent, over persistent WebSockets: agent
  socket plus VM socket, bridged by the relay within one tenant.
- No blind joins: every socket is authenticated before it is
  bridged. No socket, no pairing, no tool call without login.
- Relay blindness for secrets: tool traffic is encrypted
  end-to-end between VM and agent; the relay forwards
  ciphertext, plus public keys and public bootstrap material only.
- No repo sharing, no package server, no separate download:
  the desktop agent itself sends the VM-side tools in the
  bootstrap response (§7).
- Minimal VM-side tooling: three tools only (§9), plain and
  lightweight. MCP is optional, never required, never in the
  relay (§9).
- Driving model unchanged: the assistant decides tool plus
  arguments, the Mac agent runs it through its existing
  HTTP+JSON surface, and returns the result. Transport-only
  migration from the Tailscale era; zero tool changes on the
  agent.
- Owner-managed credentials and kill switch.

---

## 3. Non-goals (v1)

- No direct peer-to-peer connection (impossible for this pair).
  No UPnP port mapping in v1.
- No MCP anywhere in the relay, and no MCP endpoint on the
  relay. The relay speaks tunnel frames only (§11).
- No hosted admin web app. Administration is the localhost
  page on the Mac (§12).
- No repo sharing, no public package registry as a dependency,
  and no one-click store distribution for strangers. A stranger
  still needs pairing info from the owner (§6); that is out of
  scope for v1.
- No hand-rolled crypto, ever. Standard, audited constructions
  only (§8, §10).
- No build, deployment, installation, or Mac contact under
  this document. This document is documentation only.

---

## 4. Architecture at a glance

Three boxes. Nothing else is in the path.

| Box | Where | What it holds | What it never holds |
| --- | --- | --- | --- |
| VM helpers | Sandbox VM, same machine as the assistant runtime | VM keypair (private, mode-600 file), agent public key (pinned), relay login (vault-injected), tools code (sent by the agent) | Agent private key, agent relay password |
| Relay | Fly.io, small VPS, or owner GCP (host still open, §16) | Paired sockets, public keys (directory, §10), public bootstrap material in transit | Private keys, tool requests/results in plaintext, session keys |
| Desktop agent | Mac (Mac mini, Mac Studio, MacBook) | Agent keypair (private in Keychain), VM public key (pinned), relay login (Keychain), full HTTP+JSON tool surface | VM private key, VM relay password |

Flow, steady state:

1. Assistant calls a VM helper for one agent action.
2. Helper encrypts the request with the agent's public key
   (hybrid construction, §8) and sends it as a DATA frame
   over the paired WebSocket.
3. Relay forwards the ciphertext to the agent's socket.
   It does not parse it.
4. Agent decrypts with its private key, validates the
   request shape, executes the tool, encrypts the response
   with the VM's public key, and sends it back the same way.
5. Helper decrypts with the VM private key and returns the
   plain result to the assistant.

The assistant never sees ciphertext, never calls encrypt or
decrypt as separate tools, and never holds keys.

---

## 5. What each side holds

### 5.1 VM side

- VM keypair, generated on the VM, never elsewhere, never
  transmitted. Private key as a mode-600 file under
  `~/.ice-vm/` (home persists across VM replacements, so no
  re-pairing on restart). The Secure Vault cannot hold it:
  the vault takes only user-entered credentials and never
  hands stored values back to agent code. Honest limit: file
  permissions stop other in-VM users and processes, but the
  host operator could read the disk. VM-key confidentiality
  rests on runtime trust, the same trust already placed in
  Muse holding chats and credentials. The Mac side is
  strictly stronger.
- Agent public key, pinned after bootstrap, verified by
  fingerprint before first sensitive use (§7).
- Relay login: client ID plus password, service-generated,
  stored in the Secure Vault as a custom connector setup,
  injected by the runtime at helper start. Login yields a
  short-lived auth token, which yields the WebSocket.
  The relay login doubles as the kill switch: disable the
  user or revoke tokens at the service, and all access stops
  without touching keys or the Mac.
- Tools code, sent by the agent in bootstrap (§7), saved
  on the VM, inspected before first use.

### 5.2 Relay side

- Paired sockets, one bridge per agent ID per tenant.
- A public-key directory (§10): current public key per
  client ID, plus the signature that authenticates a rotation.
- Public bootstrap material in transit during first contact
  only (agent.md, tools code, public keys). All of it is
  public by design.
- Nothing secret. No private keys, no passwords in plaintext,
  no tool requests or results in plaintext, no session keys.

### 5.3 Mac side

- Agent keypair. Private key in the macOS Keychain
  (non-exportable, Secure-Enclave-backed where available).
  Password in the Keychain. Never on the filesystem in
  plaintext. Filesystem may hold only public values:
  public key plus client ID.
- VM public key, pinned after bootstrap, shown in the
  trusted-VM field of the agent identity screen.
- Relay login in the Keychain. Agent connects on every
  open: Keychain login, token, persistent WebSocket,
  register agent ID.
- Identity screen (in the agent app, not the installer,
  which shows nothing): agent ID, public key box with Copy,
  Rotate button, token in a revealable field, trusted-VM
  field. Rotation updates the Keychain; agent ID is
  unchanged (§10).
- The full existing tool surface, unchanged: shell exec,
  file operations, screenshot, accessibility tree, mouse,
  keyboard, app launch and driving, and everything the
  Tailscale-era agent already exposed over HTTP+JSON.
  The agent implements these; VM helpers never
  re-implement them.

---

## 6. User-facing setup

From the user's side, setup is the product model, not an
install:

1. Owner provides pairing info once: relay URL, agent ID,
   and relay login. The login goes into the Secure Vault
   as a custom connector setup. The user does not install
   a server, does not copy a folder, and does not type a
   server URL into Settings.
2. Assistant pairs to the agent through the relay with
   that login (token, then WebSocket, then pairing within
   the tenant). Pairing is tenant-scoped; identities come
   from validated tokens, never from self-asserted IDs.
3. Bootstrap runs (§7). The agent sends agent.md, tools
   code, and its public key. No repo, no download, no
   separate install step to discover.
4. Owner verifies the fingerprint once, from the agent
   identity screen against what the VM received, before
   first sensitive use. Until that verification, the VM
   treats the pairing as unpinned and must not run
   sensitive tools.
5. After that, use is normal: assistant calls helpers,
   helpers drive the agent, relay stays blind to secrets.

For another person, the same flow applies, with pairing
info from the owner of that agent. There is no store
listing and no shared repo in v1; that is an accepted
limit, not an oversight (§3).

---

## 7. Bootstrap: agent.md plus tools, sent by the agent

This section is the core of the revision. It replaces
repo sharing, package download, and llms.txt discovery
with a single in-band bootstrap.

### 7.1 First contact

Prerequisite: pairing already established (§6 steps 1-2).
The VM is paired to the agent through the relay, both
sides authenticated to the relay with their tenant logins.
Bootstrap plaintext therefore runs inside an authenticated
relay pairing, not on the open internet.

Steps:

1. VM sends a plaintext first-contact request to the
   agent through the paired relay socket. Plaintext here
   means only: "send agent.md and tools." It contains no
   tool request, no secret, no credential.
2. Agent replies in plaintext with exactly three things:
   - agent.md: plain-text instructions for the VM side:
     what the agent is, the tool list, how to save and
     use the tools, how key exchange and rotation work,
     and the rules in §7.2 and §8.
   - Tools code: the small VM-side helpers (§9), as
     plain-text source, using standard libraries only.
   - Agent public key, plus its fingerprint.
3. VM saves agent.md and the tools code, and inspects
   the tools code before running it. Inspection is
   mandatory: code received over a channel is read
   before it is executed, the same rule as for any
   helper script.
4. VM generates its own keypair (if it does not already
   have one), and sends its public key to the agent
   through the paired relay socket. A public key is
   public; sending it in plaintext at this stage is
   acceptable, because the pairing itself is
   authenticated by the relay logins, and the key is
   pinned by fingerprint verification in step 5.
5. Agent stores the VM public key as its trusted-VM key
   and shows it in the trusted-VM field. Owner verifies
   fingerprints both ways (agent fingerprint on the VM
   side against the agent identity screen; VM fingerprint
   on the agent side against what the VM sent) before
   first sensitive use.
6. VM switches to encrypted mode (§8). Bootstrap is done.
   Later tool updates, if any, are sent encrypted over
   the established channel, never by re-running plaintext
   bootstrap for sensitive work.

### 7.2 The plaintext exception rule (formal)

Agent.md is the SOLE plaintext exception, in both
directions, for the life of the pairing:

- If the agent receives plaintext, it responds ONLY
  with agent.md plus tools code plus its current public
  key. It never executes a tool, never returns a tool
  result, never returns a secret, on a plaintext request.
- If the agent receives ciphertext it cannot decrypt
  (wrong key, stale key after rotation, corrupt bytes),
  it responds the same way: agent.md plus tools code
  plus its current public key, in plaintext. The VM can
  then refetch the current key, re-encrypt, and retry.
  This is also the rotation-recovery path (§10).
- Tool requests, tool results, screenshots, shell output,
  passwords, tokens, and private keys are never sent in
  plaintext, in either direction, after bootstrap.

Because agent.md, tools code, and public keys are public
by design, the relay seeing them in bootstrap does not
break secret-blindness. The relay still never sees a
tool request or result in plaintext.

### 7.3 Why this replaces llms.txt discovery

A fresh VM cannot be expected to already know that a
documentation URL exists, or what it is. Asking "how
will you know there is an llms.txt" has no good answer
without another out-of-band channel. Agent-sent bootstrap
removes the question: the agent itself answers discovery,
on first contact, with the instructions and the tools.
A service-hosted agent.md or llms.txt at a service root
remains a valid alternative discovery path for other
services, and the robots.txt analogy still holds
(robots.txt for crawlers, llms.txt and AGENTS.md for
agents), but it is not needed here, because the agent
is the discovery endpoint.

---

## 8. Steady-state encryption

After bootstrap, every tool message is encrypted at the
message level with the recipient's long-term public key,
using a standard hybrid construction:

- VM to agent: encrypt the request with the agent's
  public key. Agent decrypts with its private key.
- Agent to VM: encrypt the response with the VM's
  public key. VM decrypts with its private key.
- Hybrid means: a fresh random symmetric key per
  message encrypts the payload, and that symmetric key
  is wrapped with the recipient's public key. Never
  raw asymmetric encryption of a large payload (for
  example, a screenshot) directly with a public key.
- Standard, audited libraries only. No hand-rolled
  crypto, no custom cipher modes, no model-invoked
  encrypt or decrypt steps. The helper does all of it
  inside one call; the assistant sees only the plain
  result.

Honest tradeoff, stated plainly: direct long-term
public-key encryption does NOT provide forward secrecy.
If a private key later leaks, recorded past ciphertext
could be decrypted with it. Rotation (§10) bounds that
exposure in time, but does not remove it. The
forward-secret alternative, for a later revision if the
owner wants it, is to use long-term keys only to sign
an ephemeral key exchange per session (which is, in
essence, what mutual TLS does). That is a decision owed
for a future revision, not a v1 requirement, and it
does not change the bootstrap, tool set, or relay shape
described here. (The locked doc's mutual-TLS design is
superseded on this point.)

---

## 9. The VM-side tools: three only, MCP optional

VM-side tooling is three tools, plain and lightweight:

1. **Send (encrypt and send).** Takes a tool name plus
   arguments, encrypts the request with the agent's
   public key, sends it as a DATA frame over the paired
   WebSocket, waits for the response, and returns it
   (see tool 2, which is normally invoked together
   with this one inside a single helper call).
2. **Receive (receive and decrypt).** Takes the
   response frame, decrypts it with the VM private key,
   and returns the plain result. In practice, tools 1
   and 2 are the two halves of one helper invocation
   per agent action, not two separate assistant calls.
   The assistant makes one call ("screenshot", with
   arguments) and gets back the plain screenshot. It
   never sees ciphertext, and it never calls encrypt
   or decrypt on its own. Exposing encrypt and decrypt
   as separate assistant tools is explicitly rejected:
   it would push ciphertext through the assistant's
   context (tens of thousands of tokens per screenshot)
   and run a crypto state machine through reasoning,
   for zero security gain, since the helper must hold
   the keys and the plaintext anyway.
3. **Rotate keys.** Generates a fresh keypair for the
   calling side, publishes the new public key as in
   §10, retires the old private key, and keeps the
   agent ID and relay login unchanged. Rotation does
   not rotate the relay password.

MCP status: MCP is optional and, if used at all, is
only a VM-side interface. An MCP server process on the
VM could expose the same three helpers as MCP tools
(name, description, schema), with the same
encrypt-inside-the-helper behaviour. MCP never touches
the relay, never appears on the relay, and never changes
security, transport, or the tool set. Plain CLI helpers
are sufficient for v1 and are preferred for lightness:
direct commands, less protocol, easier to debug. The
choice between plain helpers and MCP does not matter
to the rest of this design, and it can be made at build
time without revisiting the relay, bootstrap, or crypto
sections.

---

## 10. Key management and rotation

- Agent ID is stable across rotation. Rotation changes
  keys, not identity, and not the relay login.
- VM private key: mode-600 file under `~/.ice-vm/`,
  as in §5.1. Agent private key: Keychain, as in §5.3.
  Private keys are never transmitted, never logged,
  never placed in chat, memory, or ordinary files.
- Public keys are public. Either side may hold the
  other's public key, and the relay may hold both as a
  directory, without breaking secret-blindness.
- Rotation protocol (both directions symmetric):
  1. Rotating side generates a fresh keypair and
     stores the new private key (Keychain on the Mac,
     mode-600 file on the VM).
  2. Rotating side publishes the new public key to
     the relay directory, authenticated with its relay
     login, and signed with its OLD private key.
  3. Peer fetches the new public key from the relay,
     verifies the signature with the OLD public key,
     and only then replaces its pinned key.
  4. Peer confirms (encrypted with the new key, so
     the rotating side knows the peer has it).
  5. Old private key is retired and deleted.
  Signature with the old key is what prevents a relay
  compromise from swapping in an attacker's key: the
  relay can withhold a rotation, but it cannot forge
  the old-key signature.
- Rotation failure is fail-closed and self-healing:
  a peer encrypting with a stale key gets a decrypt
  failure, the agent replies with agent.md plus its
  current public key (§7.2), the peer refetches and
  retries. A rotated side is unreachable until the
  peer updates its pinned key; it never falls back to
  plaintext tool execution.
- Fingerprint rule: a fingerprint is a short hash of
  the public key, not the key itself. Public keys and
  fingerprints are safe to share; a fingerprint alone
  is a claim, the private key is the proof. Owner
  fingerprint verification (§6 step 4, §7.1 step 5)
  is required before first sensitive use and after
  every rotation that the owner did not initiate
  through the agent UI.

---

## 11. Relay protocol

The relay is a dumb, blind forwarder. It speaks tunnel
frames only, and it speaks them the same way in both
VM-side variants (plain helpers or MCP):

- LOGIN: client ID plus password, over TLS to the
  relay. Relay verifies against password hashes only
  (plaintext shown once at creation, then purged).
  Success yields a short-lived auth token.
- TOKEN: token presentation to open the WebSocket.
  The authenticated socket is the proof for the
  session; tool messages carry no further credentials.
  Token expiry or revocation closes the socket and
  forces re-login.
- PAIR: relay pairs the VM socket and the agent socket
  with the same agent ID within the same tenant, and
  only those two. No cross-tenant pairing, ever.
  Identities come from validated tokens, never from
  self-asserted IDs.
- DATA: opaque bytes forwarded from one paired socket
  to the other. In steady state these bytes are
  ciphertext. In bootstrap they are public agent.md,
  tools code, and public keys. The relay never parses
  DATA, never parses MCP, never parses HTTP+JSON tool
  content.
- KEY_PUBLISH / KEY_FETCH: relay directory operations
  for §10. Authenticated, signed as specified there.
  Relay stores public keys and signatures only.

Relay behaviour:

- Single controller per agent ID per tenant. A second
  controller attempting to pair to the same agent ID
  gets AGENT_BUSY. An unpaired or disconnected agent
  yields AGENT_OFFLINE immediately, not a hang.
- Slot locks only for authenticated bridges. Relay
  rate-limits login and pairing per client ID.
- Auto-reconnect on drop, on both sides, with
  backoff. Reconnect re-presents a still-valid token,
  else fresh login. A dropped socket has unknown
  outcome for any in-flight request: the helper must
  not assume success or failure, and must surface
  that unknown outcome honestly (TUNNEL_DROPPED
  semantics).
- Relay compromise yields, at most: ciphertext it
  cannot decrypt, public keys, public agent.md and
  tools code, and metadata (IDs, timing, sizes). It
  yields no tool requests or results in plaintext, no
  private keys, no passwords, and no session keys.
  It can withhold or tamper with bootstrap plaintext,
  which is why §7.1 step 3 (inspect tools code) and
  §6 step 4 (fingerprint verification) are mandatory,
  not optional.

---

## 12. Administration

Administration is the localhost one-page interface
served by the desktop agent, on 127.0.0.1 only, never
0.0.0.0, behind a local gate token (localhost alone is
not authentication). It shows: agent connected, VM
bridged, last-seen; it drives password resets and the
kill switch through the agent's authenticated channel.
It is never exposed via the relay. Trade-off: manageable
only from the Mac.

Password reset rule: the service requires the current
password plus client ID, and issues a new password. The
agent's own new password goes straight into the Keychain;
a VM login's new password is shown once for the owner to
copy into the vault. Service-generated passwords
throughout; the owner never invents them. Kill switch:
disable the user or revoke tokens at the service, and
all access stops without touching keys or the Mac.

---

## 13. Migration and rollback

Migration is transport-only: the agent's config, token
history, and tools are untouched except for the transport
change. The agent gains relay mode (relay login, token,
WebSocket, Keychain password, plaintext bootstrap
handler, hybrid encrypt/decrypt, localhost page) and
drops Tailscale. The driving model and the tool surface
do not change.

Steps (for a future build, not now):

1. Build the relay service plus auth backend.
2. Build the agent's relay mode.
3. Build the VM helpers (the three tools, §9).
4. Prove the legs on a local stack, then against the
   deployed relay, with screenshots.
5. Remove Tailscale from both sides after the legs pass.
   The tunnel-uninstall reminder is closed by the
   migration itself, not by a separate cleanup.

Rollback: the transport flag flips back to Tailscale
mode. Rollback is a restart, not a restore — no data
migration is involved, and the agent's Keychain items
and the VM's files are untouched by the transport change.

---

## 14. Acceptance criteria (for a future build, not now)

Build is not authorized until the owner says so. When it
is authorized, it is not done until each of these is
proven end-to-end with real traffic, not by code reading
alone:

- Plaintext first contact returns ONLY agent.md plus
  tools code plus agent public key, and executes nothing.
- Plaintext tool request after bootstrap returns ONLY
  agent.md, never a tool result or secret.
- Encrypted tool call round-trips, including a
  screenshot, with the relay seeing only ciphertext
  (plus public bootstrap material).
- Assistant never sees ciphertext and never calls
  encrypt or decrypt as separate tools.
- VM key rotation and agent key rotation each work
  via signed relay-directory publish plus peer fetch,
  with fail-closed behaviour until the peer updates.
- Decrypt failure recovers via agent.md refetch and
  retry, without manual re-pairing.
- Kill switch stops all access immediately.
- Reconnect after a drop restores the bridge without
  re-pairing, and in-flight unknown outcomes are
  reported honestly, not assumed.
- Single controller enforced (second controller gets
  AGENT_BUSY); offline agent yields AGENT_OFFLINE
  within seconds, not a hang.
- Fresh Mac, no Tailscale installed anywhere: installer
  runs silently, agent registers, VM pairs, VM drives
  screenshot plus exec plus an accessibility click
  through the relay, with zero network configuration
  on either side.
- Relay-blindness proof: a relay build with full frame
  logging enabled shows ciphertext only for tool
  traffic, and a forged pairing (wrong token, wrong
  tenant) is rejected.
- No blind joins: a login with a wrong password fails
  at the door; a pairing without a valid token for that
  tenant is rejected; a duplicate client ID registration
  is rejected; a cross-tenant pairing is rejected.
- Token expiry: an expired token closes the socket
  with TOKEN_EXPIRED; the client logs in fresh and
  re-pairs with no manual step.
- Localhost page: shows agent and VM connection
  status; password reset works (agent's own automatic,
  VM login shown once for vault update); kill switch
  (disable user) drops all of that user's sockets
  immediately.
- TCC: owner re-grant performed once after the new
  agent identity is installed; agent tools work after it.
- Tailscale fully removed from both sides after the
  legs pass; the tunnel-uninstall reminder is closed
  by the migration, not by a separate cleanup.
- Agent repo updated source-only (relay mode plus
  docs); no binary published.

---

## 15. Step-by-step flows (diagrams)

The sequence diagrams live in `design-docs/diagrams/`
as `.mmd` sources with rendered `.png` files. Read D2
through D3 and D10 in order for one complete session,
from login to a tool call returning. In every diagram
the relay forwards bytes and stays in the path for the
whole session — there is no peer-to-peer leg. The
session is "direct" only in the sense the owner means:
one session, VM to agent, no VPN app, no network setup
on either side.

| Diagram | Flow | Status in this design |
| --- | --- | --- |
| 00-architecture | Three boxes: VM helpers, relay, desktop agent | Applies (VM box holds helpers, not an MCP server) |
| D1-provisioning | Registration, once per machine | Applies |
| D2-agent-login | Agent login on every open | Applies |
| D3-vm-pairing | VM login and pairing | Applies |
| D4-tls-handshake | End-to-end TLS handshake | **Superseded** by §8 message-level hybrid encryption; kept in the folder as history only |
| D5-password-reset | Password reset from the localhost page | Applies |
| D6-tool-call | Steady-state tool call | Applies with DATA frames carrying hybrid ciphertext (§8), not "HTTP inside TLS" |
| D7-reconnect | Drop and reconnect mid-session | Applies (TUNNEL_DROPPED semantics) |
| D8-failures | Named failure paths | Applies; the "wrong certificate inside the tunnel" case is replaced by decrypt-failure → agent.md recovery (§7.2) |
| D9 | Why the relay cannot decrypt | Superseded in its Diffie-Hellman form; the new argument is in §8 and §11: the relay holds no private keys, only ciphertext plus public material, with the forward-secrecy tradeoff stated in §8 |
| D10-pairing | How each side knows the other is real | Applies with the mutual-TLS leg replaced by public-key pinning plus owner fingerprint verification (§6 step 4, §7.1 step 5) |
| D11-localhost-page | Status, reset, kill switch | Applies |

---

## 16. Open questions for the owner

- Relay host: Fly.io, small VPS, or owner GCP?
- Relay repo home: where does the relay service live?
- Agent ID naming convention?
- Forward secrecy: accept long-term public-key message
  encryption for v1 with rotation as the bound (§8),
  or require ephemeral exchange (TLS-like) from the
  start? Decision owed before build.
- Hybrid construction and WebSocket library choice:
  to be fixed at build time, standard libraries only.
- Build go-ahead: explicitly NOT given by this
  document. Owner must say build, separately, later.

---

*Single governing document, merged 2026-10-29 from
`relay-connectivity.md` (LOCKED 2026-10-25) and
`relay-connectivity-bootstrap.md` (owner discussion
2026-10-25 ~16:09–16:26 PDT). The earlier files are kept
as history in `design-docs/history/`. Build not
authorized. Leave as-is until the owner authorizes build.*
