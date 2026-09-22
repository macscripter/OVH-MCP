# Bridge — Helm sub-chart

The Kubernetes packaging of the Simpl-Open **Bridge**: the single component of the Governance
Authority (GA) agent that talks to the DOME Marketplace. Increment 1 ships the read path — federated
real-time TMF620 v4 search, no replication, DOME stays authoritative.

This is a **sub-chart of the Governance Authority chart, gated by `bridge.enabled`** (BRG-D-09). It
is not designed to be installed on its own in a real environment: it renders no ServiceAccount, no
Ingress, no NetworkPolicy and no Secret, because those belong to the agent that hosts it (BRG-D-03 —
the Bridge runs inside the GA agent and uses one GA-level technical identity towards DOME).

| | |
|---|---|
| Chart version | `1.0.0-inc1.1` — the trailing counter moves when the chart changes without the artefact version changing, which the ArgoCD Application pins by version |
| App version | `1.0.0-INC1` (matches `pom.xml`) |
| Objects rendered | ConfigMap, Service, Deployment |
| Runtime | Java 25 on GraalVM for JDK 25, Quarkus 3.33 LTS, Apache Camel (BRG-D-04 / BRG-D-10) |

## How the values are addressed

Everything in `values.yaml` lives under the parent's `bridge:` key. A key written in this chart as

```yaml
cache:
  ttlSeconds: 300
```

is set by an operator as `bridge.cache.ttlSeconds` — the same name the design brief uses, and the
same name the application's own configuration tree uses. That is not a coincidence: `BridgeConfig`
maps the `bridge.*` prefix with `NamingStrategy.VERBATIM` precisely so that the Helm key, the
property key and the design document all read alike. Renaming one of them silently breaks that
chain.

### Wiring it into the parent chart

`Chart.yaml` of the Governance Authority chart:

```yaml
dependencies:
  - name: bridge
    version: 1.0.0-inc1.1
    repository: "file://charts/bridge"
    condition: bridge.enabled
```

`values.yaml` of the Governance Authority chart:

```yaml
bridge:
  enabled: true
  image:
    registry: registry.example.eu
    tag: 1.0.0-INC1
  dome:
    baseUrl: https://tmf.sbx.evidenceledger.eu/tmf-api/productCatalogManagement
    apiVersion: v4
  redis:
    hosts: redis://redis.common01.svc.cluster.local:6379
```

`bridge.enabled` does double duty. The dependency condition drops every manifest when it is false,
and the same value is written into `application.properties` as `bridge.enabled`, so a Bridge that is
deployed but switched off starts without its Camel routes rather than starting and failing against a
DOME it was never meant to reach.

### Reviewing it standalone

The templates repeat the gate with `{{- if .Values.enabled }}`, so the chart can be rendered on its
own for review:

```bash
helm lint charts/bridge
helm template ga charts/bridge --namespace authority01
helm template ga charts/bridge --set enabled=false     # renders nothing
```

## What the chart configures

### Southbound — DOME (BRG-D-08, BRG-D-11)

The read URL is composed, not pasted:

```
<dome.baseUrl>/<dome.apiVersion>/<dome.productOfferingPath>
```

so pinning the TM Forum version per environment stays a single key. The default resolves to
`https://tmf.sbx.evidenceledger.eu/tmf-api/productCatalogManagement/v4/productOffering`, the sandbox
verified on 2026-09-12, where **reads are anonymous**.

| Key | Default | Meaning |
|---|---|---|
| `dome.baseUrl` | `https://tmf.sbx.evidenceledger.eu/tmf-api/productCatalogManagement` | Catalogue root, **without** a version segment |
| `dome.api` | `TMF620` | Reported back on every search response as `source.api` |
| `dome.apiVersion` | `v4` | The BRG-D-11 pin; reported as `source.apiVersion` |
| `dome.productOfferingPath` | `productOffering` | Collection searched on the read path |
| `dome.propagateCorrelationId` | `true` | Forwards `X-Correlation-Id` southbound. A correlation aid only — no caller credential ever goes to DOME |

### Read path budget (`search.*`, `resilience.*`)

DOME's measured latency tracks the **offset**, not the page size: 1.8 s at offset 0, 11.4 s at
offset 127, 13.2 s past the end of the collection. The backend scans from the start on every
request, so one flat timeout is generous on page 1 and hopeless on page 40. The chart exposes both
halves of the answer:

* `search.maxOffset` (default **2000**, contract ceiling 10000) — a caller-side guardrail. Exceeding
  it is a `400`, which says nothing about DOME's availability.
* `resilience.timeout.offsetScaling` (default `true`) — the per-attempt budget grows with depth
  (`baseMs + offset × perOffsetMs`, capped at `maxMs`) instead of being flat.
* `resilience.retry.retryOnTimeout` (default `false`) — at depth the retry costs the same again and
  returns the same answer.

`search.maxLimit` and `search.maxOffset` are validated against the northbound contract's own
ceilings (100 and 10000) at render time; a value above them fails `helm template`, rather than
producing a Bridge that advertises more than the API promises.

### Search never fails the caller

This is the load-bearing behaviour of Increment 1, and two decisions in this chart protect it.

**Readiness does not depend on anything external.** `quarkus.redis.health.enabled` and
`quarkus.elasticsearch.health.enabled` are set to `false` in the rendered properties. Both
extensions register readiness checks by default, and either one going red would take the pod out of
the Service — turning "DOME results unavailable, `degraded: true`, HTTP 200" into "no Bridge at
all", which is exactly the outcome the design forbids. A cache fault is a miss; the audit sink is
not on the request path.

**The pod outlives its own timeout budget.** `terminationGracePeriodSeconds` (45 s) is longer than
`resilience.timeout.maxMs` (20 s) plus the Quarkus shutdown timeout, so a rolling update never
truncates a live DOME search. The chart fails to render if someone shortens the grace period below
the timeout budget.

### Probes

| Probe | Path | Port |
|---|---|---|
| Startup | `/q/health/started` | management (9000), or http when `management.enabled=false` |
| Readiness | `/q/health/ready` | idem |
| Liveness | `/q/health/live` | idem |

The startup probe absorbs JVM start-up (up to 60 s at the default 2 s × 30), which is what lets
liveness and readiness be impatient afterwards. All timings are in `probes.*`.

With `management.enabled: true` (the default) the health endpoints are served on the Quarkus
management interface on port 9000, off the application port. The probes follow the setting
automatically; the Service exposes the management port only if `service.exposeManagement` is set.

### Read cache (BRG-D-12)

Cache-aside with TTL in Redis. A cached page can be up to `cache.ttlSeconds` (300) stale; DOME stays
authoritative, and nothing is ever served that DOME did not return. `cache.keyPrefix` carries the
northbound contract version (`simpl:bridge:v1:…`) so a future v2 cannot read v1 entries.

Setting `cache.enabled: true` with an empty `redis.hosts` fails the render — that combination
produces a Bridge that logs a cache miss on every single search and looks healthy while doing it.

### Observability (BRG-D-13) — ELK only

* JSON logs to stdout (`quarkus-logging-json`), for Filebeat.
* Camel audit route to Elasticsearch at `elk.elasticsearch.hosts`.
* Micrometer with the **Elastic** registry, configured under `bridge.metrics.elastic.*`.

No Prometheus, no OTLP. Two consequences worth stating rather than discovering:

1. There is no Quarkus extension for the Micrometer Elastic registry (Quarkus ships Prometheus;
   Quarkiverse covers Datadog, Influx, OTLP and others, not Elastic), so the registry is a plain
   Micrometer bean produced by the application — `ElasticMeterRegistryProducer` — and configured
   from the `bridge.metrics.elastic.*` keys this chart renders. Micrometer's own `elastic.*` options
   are all reachable under that prefix, so a key this chart does not model can still be set through
   `config.extraProperties`.
2. The Elastic registry is push-only: metrics arrive in Elasticsearch on the `elk.metrics.stepSeconds`
   interval and nothing scrapes the pod. `/q/metrics` is nevertheless served, by Micrometer's JSON
   exporter (`quarkus.micrometer.export.json.*`), which is what lets an operator read the current
   values without adding Prometheus. The probes do not depend on it.

### Secrets — OpenBao (BRG-D-14)

**No credential is ever a value in this chart.** `openbao.credentialsPath` is a *location* in
OpenBao, and it is the only thing rendered into the ConfigMap; the credential itself reaches the pod
one of two ways:

| `openbao.mode` | Mechanism |
|---|---|
| `existingSecret` (default) | A Kubernetes Secret that some operator already synchronises from `credentialsPath`. The chart references it as `secretKeyRef` entries, one per `openbao.existingSecret.keys` mapping of `ENV_VAR → secret key`. |
| `injector` | An OpenBao/Vault agent injector renders the secret into the pod filesystem, driven by `openbao.injector.annotations`. The annotation values are passed through `tpl`, so they can reference `.Values.openbao.credentialsPath`. Replace the whole map if your injector uses a different annotation domain. |

The chart refuses to render if it finds `openbao.token`, `openbao.clientSecret`, `openbao.password`,
`redis.password` or `elk.metrics.password` in the values, and points at the `existingSecret`
alternative instead.

`openbao.enabled` defaults to **false**, and that is a statement of fact rather than an oversight:
Increment 1's read path is anonymous — verified against the live sandbox — and the **write path is
not validated** (BRG-O-04). DOME's integration guide documents publication as plain REST with no
authentication described in that chapter, and the only documented machine-to-machine route is a
LEARCredentialMachine issued to Access Node operators, an architecture this design explicitly
avoids. The block is wiring for Increment 2, switched off until DOME answers the question.

Redis and Elasticsearch credentials follow the same rule: `redis.existingSecret` and
`elk.*.existingSecret` name a Secret, never a value.

## Resources

Sizing follows a `resourcePreset` name rather than a wall of numbers. The default is `small`, which
is modest enough for the local k3d Simpl-Open and for a small cluster generally:

| Preset | Requests | Limits |
|---|---|---|
| `nano` | 50m / 192Mi | 250m / 256Mi |
| `micro` | 100m / 256Mi | 500m / 512Mi |
| **`small`** (default) | **250m / 512Mi** | **1 / 1Gi** |
| `medium` | 500m / 1Gi | 2 / 2Gi |
| `large` | 1 / 2Gi | 4 / 4Gi |

Set `resources` explicitly to bypass the presets entirely; an unknown preset name fails the render
and lists the valid ones. `java.maxRamPercentage` (70) is what makes the memory *limit* actually
govern the heap — without it the JVM would size itself from the node rather than from the cgroup.

## Configuration delivery

The whole `bridge.*` tree is rendered into one `application.properties` in the ConfigMap, mounted
read-only at `config.mountPath` and loaded through `SMALLRYE_CONFIG_LOCATIONS`. The file carries
`config_ordinal=275`, which places it above the properties baked into the image (250) and below
environment variables (300) — so an operator can still override a single key through `extraEnv` in
an emergency without re-rendering the chart.

The Deployment carries a `checksum/config` annotation over the rendered ConfigMap, so a
`helm upgrade` that changes only a timeout budget still rolls the pods.

Anything this chart does not model goes in `config.extraProperties` (appended verbatim) or
`extraEnv`. Not secrets.

## Hardening

The pod runs as non-root uid 185 (the Quarkus base-image user) with a read-only root filesystem, all
capabilities dropped, no privilege escalation and the `RuntimeDefault` seccomp profile. `/tmp` is an
in-memory `emptyDir` and `java.io.tmpdir` points at it, which is what makes the read-only root
workable.

The Service is `ClusterIP`. The Bridge is reachable on the internal agent network only; northbound
authentication is the agent's own Keycloak, and nothing here should make edge exposure easy by
accident.

## Naming

`fullnameOverride` defaults to `bridge`, not to `<release>-bridge`. The northbound contract
(`openapi/bridge-v1.yaml`) documents the in-cluster server as `bridge.<namespace>.svc.cluster.local`,
and a GA agent hosts exactly one Bridge, so the release-prefixed name would only make the documented
host wrong. Clear the override to get the conventional behaviour back.

## Render-time validation

`helm template` fails, with a message naming the key, when:

* a credential appears in the values instead of a Secret reference;
* `search.maxLimit`, `search.defaultLimit` or `search.maxOffset` exceeds the northbound contract;
* `search.keywordStrategy` is not one of `NAME_OR_DESCRIPTION_CONTAINS`, `NAME_CONTAINS`, `NAME_EXACT`, `NAME_REGEX`, `UNSUPPORTED`;
* `dome.auth.mode` is `VERIFIER` without `openbao.enabled`, or with `openbao.mode: existingSecret` whose `keys` do not map `BRIDGE_DOME_AUTH_PRIVATEKEYJWK` and `BRIDGE_DOME_AUTH_MACHINECREDENTIAL`;
* `search.timeoutMs` is not positive, or `resilience.timeout.maxMs < baseMs`;
* `terminationGracePeriodSeconds` is shorter than `resilience.timeout.maxMs`;
* `cache.enabled` is true with no `redis.hosts`;
* `openbao.enabled` is true with no `credentialsPath`, an unknown `mode`, or a mode whose own
  configuration is missing;
* `resourcePreset` names a preset that does not exist.

These are the mistakes that otherwise produce a Bridge that starts, passes its probes and is quietly
wrong.

## Known gaps

* **The image this chart deploys is built by CI** from the Dockerfile at the repository root
  (BRG-D-10). It can equally be built locally with a JDK 25.
* **The write path is unvalidated** (BRG-O-04). The publication endpoints exist in the contract and
  the `publication.*` and `openbao.*` keys exist here, but Increment 1 exercises neither.
* **`bridge.metrics.elastic.*` is provisional** — see the observability note above.
* The Query Mapper Adapter, the northbound caller of BRG-D-05, is **not part of GA v4.0.4**; the
  integration point has to be confirmed against the release that actually carries it.
