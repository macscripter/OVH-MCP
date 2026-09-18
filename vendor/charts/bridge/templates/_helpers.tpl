{{/*
=======================================================================================
Simpl-Open — Governance Authority — Bridge
Template helpers
=======================================================================================
*/}}

{{/*
Chart name, overridable.
*/}}
{{- define "bridge.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified object name.

`fullnameOverride` defaults to `bridge` (see values.yaml): the northbound contract
documents the in-cluster host as `bridge.<namespace>.svc.cluster.local`, and a GA agent
hosts exactly one Bridge (BRG-D-03). Clearing the override restores the conventional
`<release>-<chart>` behaviour.
*/}}
{{- define "bridge.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart label value, e.g. bridge-1.0.0-inc1.
*/}}
{{- define "bridge.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Selector labels — the immutable subset. Never add anything here that changes between
releases: it goes into an immutable Deployment selector.
*/}}
{{- define "bridge.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bridge.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Full label set.
*/}}
{{- define "bridge.labels" -}}
{{ include "bridge.selectorLabels" . }}
helm.sh/chart: {{ include "bridge.chart" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/component: bridge
app.kubernetes.io/part-of: governance-authority
app.kubernetes.io/managed-by: {{ .Release.Service }}
simpl.europa.eu/agent: governance-authority
simpl.europa.eu/component: bridge
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
ServiceAccount to run as. Empty means the namespace default; this sub-chart renders no
ServiceAccount of its own, the parent GA chart owns the agent's identity.
*/}}
{{- define "bridge.serviceAccountName" -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}

{{/*
Container image reference. A digest, when set, wins over the tag.
*/}}
{{- define "bridge.image" -}}
{{- $registry := .Values.image.registry | default "" -}}
{{- $repository := required "bridge: image.repository is required" .Values.image.repository -}}
{{- $name := $repository -}}
{{- if $registry -}}
{{- $name = printf "%s/%s" (trimSuffix "/" $registry) $repository -}}
{{- end -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" $name .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" $name (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}

{{/*
Resources: an explicit `resources` map wins; otherwise the named `resourcePreset`.
*/}}
{{- define "bridge.resources" -}}
{{- if .Values.resources -}}
{{- toYaml .Values.resources -}}
{{- else -}}
{{- $preset := .Values.resourcePreset | default "small" -}}
{{- $chosen := get .Values.resourcePresets $preset -}}
{{- if not $chosen -}}
{{- fail (printf "bridge: unknown resourcePreset %q — choose one of: %s (or set resources explicitly)" $preset (keys .Values.resourcePresets | sortAlpha | join ", ")) -}}
{{- end -}}
{{- toYaml $chosen -}}
{{- end -}}
{{- end -}}

{{/*
Port the Kubernetes probes talk to. /q/health/live and /q/health/ready are served by the
Quarkus management interface when it is enabled, and by the application port when it is
not; the probes follow whichever is in use.
*/}}
{{- define "bridge.probePortName" -}}
{{- if .Values.management.enabled -}}management{{- else -}}http{{- end -}}
{{- end -}}

{{/*
Effective southbound read URL, for logging and for the README. The version segment comes
from `dome.apiVersion` so BRG-D-08's per-environment pin stays one key.
*/}}
{{- define "bridge.domeEndpoint" -}}
{{- $base := trimSuffix "/" .Values.dome.baseUrl -}}
{{- $version := trimAll "/" .Values.dome.apiVersion -}}
{{- $path := trimAll "/" .Values.dome.productOfferingPath -}}
{{- printf "%s/%s/%s" $base $version $path -}}
{{- end -}}

{{/*
OpenBao agent-injector pod annotations, passed through `tpl` so the values may reference
`.Values.openbao.credentialsPath` and friends.
*/}}
{{- define "bridge.openbaoInjectorAnnotations" -}}
{{- $ctx := . -}}
{{- range $key, $value := .Values.openbao.injector.annotations }}
{{ $key }}: {{ tpl (toString $value) $ctx | quote }}
{{- end }}
{{- end -}}

{{/*
=======================================================================================
Validation

Render-time failures for the mistakes that would otherwise surface as a running but wrong
Bridge: a search that silently exceeds the contract, a cache with nowhere to go, an
OpenBao block that names no path, or a credential typed into a values file.
=======================================================================================
*/}}
{{- define "bridge.validate" -}}

{{- /* Secrets must never be values (BRG-D-14). */ -}}
{{- if or .Values.openbao.token .Values.openbao.clientSecret .Values.openbao.password -}}
{{- fail "bridge: openbao.token / openbao.clientSecret / openbao.password are not supported — credentials come from OpenBao at bridge.openbao.credentialsPath, or from an existing Secret via bridge.openbao.existingSecret." -}}
{{- end -}}
{{- if .Values.redis.password -}}
{{- fail "bridge: redis.password is not supported — reference an existing Secret with bridge.redis.existingSecret.name instead." -}}
{{- end -}}
{{- if .Values.elk.metrics.password -}}
{{- fail "bridge: elk.metrics.password is not supported — reference an existing Secret with bridge.elk.metrics.existingSecret.name instead." -}}
{{- end -}}

{{- /* Northbound contract caps (openapi/bridge-v1.yaml, schema Paging). */ -}}
{{- if or (lt (int .Values.search.maxLimit) 1) (gt (int .Values.search.maxLimit) 100) -}}
{{- fail (printf "bridge: search.maxLimit must be between 1 and 100 (the northbound contract's ceiling); got %v" .Values.search.maxLimit) -}}
{{- end -}}
{{- if or (lt (int .Values.search.defaultLimit) 1) (gt (int .Values.search.defaultLimit) (int .Values.search.maxLimit)) -}}
{{- fail (printf "bridge: search.defaultLimit must be between 1 and search.maxLimit (%v); got %v" .Values.search.maxLimit .Values.search.defaultLimit) -}}
{{- end -}}
{{- if or (lt (int .Values.search.maxOffset) 0) (gt (int .Values.search.maxOffset) 10000) -}}
{{- fail (printf "bridge: search.maxOffset must be between 0 and 10000 (the northbound contract's ceiling); got %v" .Values.search.maxOffset) -}}
{{- end -}}
{{- if not (has .Values.search.keywordStrategy (list "NAME_EXACT" "NAME_REGEX" "UNSUPPORTED")) -}}
{{- fail (printf "bridge: search.keywordStrategy must be NAME_EXACT, NAME_REGEX or UNSUPPORTED; got %q" .Values.search.keywordStrategy) -}}
{{- end -}}

{{- /* A timeout budget that cannot expire is not a budget. */ -}}
{{- if lt (int .Values.search.timeoutMs) 1 -}}
{{- fail (printf "bridge: search.timeoutMs must be positive; got %v" .Values.search.timeoutMs) -}}
{{- end -}}
{{- if lt (int .Values.resilience.timeout.maxMs) (int .Values.resilience.timeout.baseMs) -}}
{{- fail (printf "bridge: resilience.timeout.maxMs (%v) must be at least resilience.timeout.baseMs (%v)" .Values.resilience.timeout.maxMs .Values.resilience.timeout.baseMs) -}}
{{- end -}}

{{- /* The pod must outlive the longest southbound attempt it may be holding. */ -}}
{{- $graceMs := mul (int .Values.terminationGracePeriodSeconds) 1000 -}}
{{- if lt $graceMs (int .Values.resilience.timeout.maxMs) -}}
{{- fail (printf "bridge: terminationGracePeriodSeconds (%vs) is shorter than resilience.timeout.maxMs (%vms); a rollout would truncate in-flight DOME searches" .Values.terminationGracePeriodSeconds .Values.resilience.timeout.maxMs) -}}
{{- end -}}

{{- /* A cache with no Redis behind it would degrade every search into a cache miss plus a log line. */ -}}
{{- if and .Values.cache.enabled (not .Values.redis.hosts) -}}
{{- fail "bridge: cache.enabled is true but redis.hosts is empty — set bridge.redis.hosts, or set bridge.cache.enabled=false (BRG-D-12)." -}}
{{- end -}}

{{- /* OpenBao (BRG-D-14). */ -}}
{{- if .Values.openbao.enabled -}}
{{- if not .Values.openbao.credentialsPath -}}
{{- fail "bridge: openbao.enabled is true but openbao.credentialsPath is empty (BRG-D-14)." -}}
{{- end -}}
{{- if not (has .Values.openbao.mode (list "existingSecret" "injector")) -}}
{{- fail (printf "bridge: openbao.mode must be existingSecret or injector; got %q" .Values.openbao.mode) -}}
{{- end -}}
{{- if and (eq .Values.openbao.mode "existingSecret") (not .Values.openbao.existingSecret.name) -}}
{{- fail "bridge: openbao.mode is existingSecret but openbao.existingSecret.name is empty." -}}
{{- end -}}
{{- if and (eq .Values.openbao.mode "injector") (not .Values.openbao.injector.annotations) -}}
{{- fail "bridge: openbao.mode is injector but openbao.injector.annotations is empty." -}}
{{- end -}}
{{- end -}}

{{- end -}}
