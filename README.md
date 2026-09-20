# simpl-ovh-mcp

An MCP server that deploys and operates **Simpl-Open** — and the **DOME Bridge** — on
**OVHcloud Managed Kubernetes**, from a conversation.

It is both halves of the design we settled on: the generic infrastructure surface (OVH,
Kubernetes, Helm, ArgoCD) *and* a Simpl-specific layer that knows what the platform is, in
what order it goes up, and how it fails. The generic tools are underneath, so anything the
domain layer did not anticipate is still reachable.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  simpl_*  /  bridge_*     plan · bootstrap · install · verify · diagnose      │
│                           initialise the authority · deploy the Bridge        │
├──────────────────────────────────────────────────────────────────────────────┤
│  helm_*                   argocd_*                                            │
│  the four platform charts the App-of-Apps that installs Simpl-Open            │
├──────────────────────────────────────────────────────────────────────────────┤
│  k8s_*                    objects · logs · events · storage · service proxy    │
├──────────────────────────────────────────────────────────────────────────────┤
│  ovh_*                    clusters · node pools · DNS · registries · raw API   │
└──────────────────────────────────────────────────────────────────────────────┘
```

**85 tools**, three resources, three prompts. Python 3.11+, FastMCP 4, no kubectl.

---

## Why it is shaped like this

Three decisions carry most of the design.

**Everything Simpl-Open publishes is deployed through ArgoCD, not Helm.** The platform is
an App-of-Apps: two Applications you create by hand generate dozens more. A Helm release
this server made behind ArgoCD's back would be reverted on the next sync. Helm is used only
for the four charts that must exist before ArgoCD does — ingress-nginx, cert-manager,
ArgoCD itself, and an NFS provisioner.

**In-cluster services are reached through the Kubernetes API server's proxy.** ArgoCD's
API, the Governance Authority's identity and authentication providers, the Bridge's health
and search endpoints — all of them are ClusterIP-only, and the documented way to reach them
is `kubectl port-forward`, which a server running on Railway cannot do. The API server's
`/api/v1/namespaces/…/services/…/proxy/…` reaches them all with the credentials the server
already holds, and nothing has to be published to the internet.

**Secrets never enter the conversation.** Kubeconfigs are written to the state volume with
mode 0600 and used from there; `k8s_get` on a Secret returns key names and byte counts;
ArgoCD's admin password is fetched, used and discarded inside one call. The audit log
redacts by key name before writing.

---

## What it knows that a generic Kubernetes MCP server does not

These are the failures that cost real days on this platform. Each one is a check in
`simpl_diagnose` and a step in `simpl_plan`:

| Trap | What it looks like | What it is |
|---|---|---|
| `rwx-openbao` | `simpl-notification` crash-loops on `can't open /vault/secrets/config.txt` | The OpenBao initialiser claims a **ReadWriteMany** volume. OVH's default Cinder class is RWO, so the claim never binds, OpenBao stays sealed, and no secrets are injected. The symptom is several components away from the cause. |
| `domain-suffix-resolution` | Pods run; front ends load and then fail every call | Components address each other by **ingress hostname**, including from inside the cluster. `domainSuffix` must resolve from a pod, not only from a browser. |
| `missing-issuers` | Ingresses come up with no certificate, and nothing says why | The charts reference two cluster issuers **by name** — `dev-prod-http` and `dev-selfsigned` — and fail quietly when they are absent. |
| `authority-not-initialised` | `tier2-gateway`, `tier2-proxy`, `users-roles` never go healthy | The tier-1 trust chain has to be created after deployment: six HTTP calls, or the chart's automatic initialisation job. |
| `wrong-project-id` | The common components chart cannot be found, or pulls `authority-iaa` | The repository's own sample deployer points `common_components` at GitLab project 1402. The chart is in **951**. |
| `hyphen-in-agent-name` | Hostnames come out malformed | Agent names become part of an FQDN. `authority-01` breaks it; `authority01` does not. |
| `install-order` | An agent's pods wait forever on databases and topics | Agents consume the common components. Install those first, and wait. |
| `cluster-too-small` | `FailedScheduling`, cluster otherwise healthy | The documented 4 vCPU / 16 GB experimental minimum does not fit sixty pods with monitoring on. |

The chart catalogue is written down and checkable: `common_components` 4.0.9 (project 951),
`authority` 4.0.4 (902), `consumer` 4.0.5 (903), `data-provider` 4.0.6 (904).
`simpl_catalog(check_published_versions=True)` reads the live GitLab Helm index — which
code.europa.eu serves anonymously — so the pinned numbers can always be compared with what
is published today.

---

## Quick start

### Local, with Claude Code (stdio)

```bash
git clone <this repo> && cd OVH-MCP
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env     # fill in the OVH credentials
```

```bash
claude mcp add simpl-ovh --env-file .env -- ./.venv/bin/python -m simpl_ovh_mcp
```

With no OVH credentials at all it still runs against any cluster you can already reach:

```bash
SIMPL_MCP_KUBECONFIG=~/.kube/config SIMPL_MCP_MODE=operate ./.venv/bin/python -m simpl_ovh_mcp
```

### Hosted, on Railway (HTTP)

Push this repository, point Railway at it (it reads the `Dockerfile`), then set:

| Variable | Value |
|---|---|
| `SIMPL_MCP_BEARER_TOKEN` | a long random string — **required**, the server refuses to serve writes without one |
| `SIMPL_MCP_ALLOWED_HOSTS` | optional — your Railway domain once it exists; setting it turns Host/Origin checking on |
| `OVH_APPLICATION_KEY` / `OVH_APPLICATION_SECRET` / `OVH_CONSUMER_KEY` | or `OVH_CLIENT_ID` / `OVH_CLIENT_SECRET` for an OAuth2 service account |
| `OVH_CLOUD_PROJECT` | the Public Cloud project id |
| `SIMPL_MCP_MODE` | `readonly`, `operate` or `admin` |

Attach a **volume at `/data`** so deployment profiles, kubeconfigs and the audit log
survive a redeploy. `SIMPL_MCP_TRANSPORT=http` and `PORT` are already handled.

Then add it to a client:

```bash
claude mcp add --transport http simpl-ovh https://simpl-mcp.up.railway.app/mcp \
  --header "Authorization: Bearer <your token>"
```

`GET /healthz` answers without authentication, for Railway's health check.

---

## Deploying Simpl-Open, end to end

```
simpl_plan ─► simpl_profile_create ─► ovh_kube_create ─► ovh_kubeconfig_fetch
   └─► simpl_bootstrap_platform ─► ovh_dns_ensure_wildcard ─► simpl_setup_issuers
        └─► simpl_setup_rwx_storage ─► simpl_install_common ─► simpl_install_agent
             └─► simpl_initialize_authority ─► simpl_status / simpl_diagnose
```

1. **`simpl_plan(agents=["authority"], sizing="demo")`** — node count and OVH flavour, the
   namespaces, the DNS records, the install order. Creates nothing.
2. **`ovh_kube_create`** then poll `ovh_kube_get` until `READY` (3–8 minutes), then
   **`ovh_kubeconfig_fetch`**, which stores the credentials on the volume and returns only
   the API server URL.
3. **`simpl_bootstrap_platform`** — the four platform charts. The OVH load balancer appears
   a few minutes later; `simpl_status` shows its address.
4. **`ovh_dns_ensure_wildcard(zone, sub_domain, target_ip)`** — `*.simpl.example.eu` at the
   load balancer. This is the step everything else depends on. Confirm with
   **`simpl_verify_dns(deep=True)`**, which resolves the name from inside a one-off pod.
5. **`simpl_setup_issuers`** (ACME over HTTP-01, or a private CA) and
   **`simpl_setup_rwx_storage`** (NFS class plus the pre-created OpenBao claim).
6. **`simpl_install_common`** — preflight-checked: ArgoCD, RWX storage, both issuers and
   DNS are verified before anything is applied. Ten to twenty minutes.
7. **`simpl_install_agent(agent_type="authority")`**, then the Bridge.

Every long step returns immediately and is watched with `simpl_status`, `simpl_wait` or
`k8s_pod_health`. An MCP call that blocks for fifteen minutes gets cut off by the client
with the work still running and nobody watching it.

### The Bridge

```
bridge_chart_info ─► bridge_render_values ─► bridge_deploy ─► bridge_status ─► bridge_search
```

`bridge_chart_info` lists what the Bridge needs from the platform — egress to DOME, a
database for the publication path, Redis, the OpenBao path, a registry, Keycloak clients,
ELK — each with what happens if the answer is no. `bridge_search` is the real test: the
northbound contract never fails because DOME failed, so a `200` proves nothing and
**`degraded: false`** proves everything. When it is degraded, `bridge_dome_probe` calls the
sandbox directly from the server and tells DOME's problem from ours.

**Publication** (Increment 2, the write path) needs two Secrets in the agent's namespace
and neither passes through a tool argument or result:

```
bridge_database_ensure ─► bridge_dome_credentials_ensure ─► bridge_deploy(publication=True,
    dome_secret="bridge-dome") ─► bridge_publish_sample
```

`bridge_database_ensure` adds a `<agent>_bridge` role and database to the platform's
postgres-operator cluster, exactly as the authority's own components get theirs, waits for
the operator to mint the password and copies it into `bridge-database` shaped for Quarkus.
`bridge_dome_credentials_ensure` writes the server's `DOME_DEV_TOKEN` variable into
`bridge-dome`. `bridge_publish_sample` registers a sample self-description (202), then
watches the operations ledger until the dispatcher has delivered it to the sandbox, and can
retire it afterwards.

The chart is vendored at `vendor/charts/bridge`; refresh it with
`scripts/sync-bridge-chart.sh` after any change in the Bridge repository.

**Source of truth for the Bridge is GitLab** —
`https://umane.emeal.nttdata.com/git/IODOMEIOSIMPLDOME/simpl-open-bridge`, branch `main`.
`github.com/macscripter/simpl-dome-bridge` is a mirror of the same commits. Build the image
from a GitLab checkout in the CI toolchain (`maven:3.9-eclipse-temurin-25`) and pass the
commit as `VCS_REF`, so `org.opencontainers.image.revision` on the running container names
the shared commit.

**The image registry lives in the OVH project, not in anyone's GitHub.** CI is disabled
on the GitLab project (`jobs_enabled: false`, no runners) and its container registry
exposes no host, so `.gitlab-ci.yml` cannot build or publish anything yet. The image is
therefore built from a GitLab checkout and pushed to an OVH Managed Private Registry in
the same Public Cloud project: `ovh_registry_create` orders it (SMALL plan, ≈€17/month),
`ovh_registry_user_create` makes a pull user and writes it straight into the cluster as an
image pull Secret — the password is never returned — and `bridge_deploy` references that
Secret. Nothing in the deployed system then depends on a personal account. The
`ghcr.io/macscripter/…` images used during the first deployment are history, not a path.

---

## Tools

| Group | Count | What is in it |
|---|---:|---|
| `meta` | 2 | `mcp_info` (mode, credentials, active profile), `mcp_audit` |
| `ovh` | 25 | projects, quotas, Kubernetes clusters and node pools, kubeconfigs, DNS zones and records, a private registry with pull Secrets written into the cluster, and `ovh_api_get` / `ovh_api_call` for everything else OVH exposes |
| `k8s` | 15 | cluster info, generic list/get/apply/delete, pod health, logs, events, storage, secret key names, service proxy, rollout restart, scale |
| `helm` | 7 | repos, releases, values, render, install, uninstall |
| `argocd` | 8 | applications, resource trees, sync, refresh, terminate, delete — all through the API server proxy |
| `simpl` | 21 | catalogue, plan, profiles, bootstrap, issuers, RWX storage, DNS verification, install, status, diagnose, wait, authority initialisation, teardown |
| `bridge` | 7 | chart info, values, deploy, status, search, DOME probe, uninstall |

Narrow the surface with `SIMPL_MCP_TOOL_GROUPS=meta,simpl,k8s` when a client's context
budget is tight.

**Resources**: `simpl://catalog`, `simpl://traps`, `simpl://runbook`, `simpl://profiles/{name}`.
**Prompts**: `deploy_simpl_open`, `diagnose_deployment`, `deploy_bridge`.

---

## Safety

Three modes, and a tool that a mode forbids is **not registered at all** — the model never
sees it, so it never spends a turn discovering the refusal.

| Mode | Can |
|---|---|
| `readonly` | inspect everything, change nothing |
| `operate` | create and change: clusters, DNS, charts, agents |
| `admin` | also destroy — and only when `SIMPL_MCP_ALLOW_DESTRUCTIVE=true` as well |

Destructive tools are two-phase. Called without `confirm`, they return the impact and a
token; called again with that token, they act. The token is bound to the exact target,
expires in ten minutes and is single-use, so "delete the test cluster" cannot be replayed
onto the production one.

Every write, allowed or refused, is appended to `audit.jsonl` on the state volume.

---

## Configuration

See `.env.example` for the full list. The ones that matter:

| Variable | Default | Notes |
|---|---|---|
| `OVH_ENDPOINT` | `ovh-eu` | `ovh-ca`, `ovh-us`, kimsufi and soyoustart are supported |
| `OVH_APPLICATION_KEY` / `_SECRET` / `OVH_CONSUMER_KEY` | — | create at <https://api.ovh.com/createToken/> with rights on `/cloud/*` and `/domain/*` |
| `OVH_CLIENT_ID` / `OVH_CLIENT_SECRET` | — | OAuth2 service account; wins over the triplet |
| `SIMPL_MCP_MODE` | `operate` | `readonly` / `operate` / `admin` |
| `SIMPL_MCP_ALLOW_DESTRUCTIVE` | `false` | second switch for deletions |
| `SIMPL_MCP_TRANSPORT` | `stdio` | `http` for a hosted deployment |
| `SIMPL_MCP_BEARER_TOKEN` | — | required for `http` unless the mode is `readonly` |
| `SIMPL_MCP_STATE_DIR` | `/data` or `./state` | profiles, kubeconfigs, audit log |
| `SIMPL_MCP_KUBECONFIG` | — | point at a cluster this server did not create |
| `BRIDGE_CHART_PATH` | vendored copy | override to deploy from a working tree |

---

## Development

```bash
.venv/bin/python -m pytest tests -q        # 60 tests, no cluster needed
docker build -t simpl-ovh-mcp .            # ~292 MB, helm 3.19 included
```

The tests cover the parts that must not be clever: the permission model and its two-phase
tokens, the Helm values that install the platform, kubeconfig parsing, OVH request signing,
the catalogue, and the heuristics that decide what a symptom means. Everything else is
exercised against a real cluster.

Verified against a live Simpl-Open install (k3d, ArgoCD 3.5.2, Kubernetes 1.35): the
Kubernetes layer, the ArgoCD layer through the API server proxy, the authority trust-chain
check, and a diagnosis that correctly identified that install's four real faults.

### Known limits

* **ArgoCD endpoint discovery** probes `http:80` then `https:443`, because whether
  `argocd-server` speaks TLS depends on how it was installed. Override with
  `ARGOCD_SCHEME` / `ARGOCD_PORT` for a non-standard install.
* **Exec-plugin kubeconfigs** (EKS, GKE) are refused rather than half-supported. Use a
  ServiceAccount token.
* `infrastructure-provider` and `application-provider` have **no published stable chart**
  yet, so `simpl_install_agent` will ask you for an explicit version.
* The Bridge chart models the **search half** of the Bridge; the publication path is wired
  through its escape hatches (`extraEnvFrom` for the datasource, `config.extraProperties`
  for the switches, `openbao.existingSecret` for the DOME token) by `bridge_deploy
  (publication=True)`. It works; it should become first-class chart values in the Bridge
  repository.
* **ACME rate limits**: Let's Encrypt allows 50 certificates per registered domain per
  week, and a full deployment asks for a few dozen. Use `acme_staging=True` while iterating.

---

## Licence

EUPL-1.2, as the Simpl-Open programme uses.
