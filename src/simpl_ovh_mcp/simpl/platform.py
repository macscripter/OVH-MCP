"""The four things that must exist before Simpl-Open can be installed, and the DNS check.

Order matters and the reasons are not obvious, so they are stated where they are enforced:

1. **ingress-nginx** — creates the OVH load balancer whose address every hostname points at.
2. **DNS** — a wildcard A record for the domain suffix. Components address each other by
   hostname from inside the cluster, so this is not cosmetic; it is the difference between
   a working platform and sixty pods that cannot find one another.
3. **cert-manager plus two named issuers** — `dev-prod-http` and `dev-selfsigned`. The
   charts reference them by name and fail quietly if they are absent.
4. **ReadWriteMany storage** — OVH's default Cinder class is ReadWriteOnce, and the OpenBao
   initialiser claims RWX. Without it the initialiser never runs and the failure surfaces
   several components away.

ArgoCD sits alongside these as the thing that installs everything else.
"""

from __future__ import annotations

import asyncio
import re
import socket
from typing import Any

from ..errors import NotFound, UpstreamError
from ..helm.runner import HelmRunner
from ..kube.client import KubeClient
from .catalog import ISSUERS, PLATFORM


async def install_platform_chart(
    helm: HelmRunner,
    key: str,
    values_override: dict[str, Any] | None = None,
    version: str | None = None,
    wait: bool = False,
) -> dict[str, Any]:
    chart = PLATFORM.get(key)
    if chart is None:
        raise NotFound(f"unknown platform chart '{key}'", f"Known: {', '.join(PLATFORM)}.")
    await helm.repo_add(chart.repo_name, chart.repo_url)
    values = dict(chart.values)
    if values_override:
        from .values import deep_merge

        deep_merge(values, values_override)
    result = await helm.upgrade_install(
        chart.release,
        f"{chart.repo_name}/{chart.chart}",
        chart.namespace,
        values=values or None,
        version=version or chart.version,
        wait=wait,
        timeout_seconds=600,
    )
    result.raise_for_status()
    return {
        "chart": chart.chart,
        "release": chart.release,
        "namespace": chart.namespace,
        "version": version or chart.version,
        "why": chart.why,
    }


# --------------------------------------------------------------------------- issuers ---
def self_signed_issuer(name: str = ISSUERS["internal"]) -> dict[str, Any]:
    return {
        "apiVersion": "cert-manager.io/v1",
        "kind": "ClusterIssuer",
        "metadata": {"name": name},
        "spec": {"selfSigned": {}},
    }


def acme_issuer(
    name: str = ISSUERS["public"],
    email: str = "",
    server: str = "https://acme-v02.api.letsencrypt.org/directory",
    ingress_class: str = "nginx",
) -> dict[str, Any]:
    return {
        "apiVersion": "cert-manager.io/v1",
        "kind": "ClusterIssuer",
        "metadata": {"name": name},
        "spec": {
            "acme": {
                "server": server,
                "email": email,
                "privateKeySecretRef": {"name": f"{name}-account-key"},
                "solvers": [{"http01": {"ingress": {"class": ingress_class}}}],
            }
        },
    }


def ca_issuer_bundle(name: str = ISSUERS["public"], namespace: str = "cert-manager") -> list[dict[str, Any]]:
    """A private CA to sign everything with, for a deployment with no public DNS.

    Browsers will warn, and so will anything that validates the chain against a public
    trust store — which is exactly what the reference laptop install lives with.
    """
    ca_secret = f"{name}-ca"
    return [
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {"name": f"{name}-bootstrap"},
            "spec": {"selfSigned": {}},
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": ca_secret, "namespace": namespace},
            "spec": {
                "isCA": True,
                "commonName": f"{name} root",
                "secretName": ca_secret,
                "duration": "43800h",
                "privateKey": {"algorithm": "ECDSA", "size": 256},
                "issuerRef": {
                    "name": f"{name}-bootstrap",
                    "kind": "ClusterIssuer",
                    "group": "cert-manager.io",
                },
            },
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {"name": name},
            "spec": {"ca": {"secretName": ca_secret}},
        },
    ]


# --------------------------------------------------------------------------- storage ---
def openbao_init_claim(common_namespace: str, storage_class: str = "nfs", size: str = "1Gi") -> dict[str, Any]:
    """Pre-create the claim the OpenBao initialiser will make, bound to RWX storage.

    The chart's own template declares this claim with no storage class, so it would be
    served by the cluster default — Cinder on OVH, which refuses ReadWriteMany. Creating it
    first with the NFS class means the chart's server-side apply finds it already there and
    leaves the class alone, because a field a manifest does not mention is a field it does
    not own.
    """
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": f"init-bao-{common_namespace}-pvc",
            "namespace": common_namespace,
            "annotations": {
                "simpl-ovh-mcp/reason": "OpenBao initialiser needs ReadWriteMany; the chart's "
                "claim carries no storage class and would land on the RWO default."
            },
        },
        "spec": {
            "accessModes": ["ReadWriteMany"],
            "storageClassName": storage_class,
            "resources": {"requests": {"storage": size}},
        },
    }


async def wait_for_storage_class(kube: KubeClient, name: str, timeout: float = 120) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        classes = await kube.list("StorageClass", limit=50)
        if any(c["metadata"]["name"] == name for c in classes):
            return True
        await asyncio.sleep(5)
    return False


async def make_default_storage_class(kube: KubeClient, name: str) -> dict[str, Any]:
    """Mark one class default and unmark the others. Blunt, and occasionally the fix."""
    classes = await kube.list("StorageClass", limit=50)
    changed = []
    for sc in classes:
        sc_name = sc["metadata"]["name"]
        is_default = (sc["metadata"].get("annotations") or {}).get(
            "storageclass.kubernetes.io/is-default-class"
        ) == "true"
        want = sc_name == name
        if is_default != want:
            await kube.patch(
                "StorageClass",
                sc_name,
                {
                    "metadata": {
                        "annotations": {
                            "storageclass.kubernetes.io/is-default-class": "true" if want else "false"
                        }
                    }
                },
            )
            changed.append({"class": sc_name, "default": want})
    return {"default": name, "changed": changed}


# ------------------------------------------------------------------------------ dns ----
async def resolve_public(hostname: str) -> list[str]:
    """Resolve a name the way the outside world does."""
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
    except socket.gaierror:
        return []
    return sorted({info[4][0] for info in infos})


async def resolve_in_cluster(
    kube: KubeClient,
    hostname: str,
    namespace: str = "default",
    image: str = "busybox:1.36",
    timeout: float = 120,
) -> dict[str, Any]:
    """Resolve a name from inside the cluster, by running one short-lived Job.

    Worth the trouble exactly once per deployment: a name that resolves from a laptop and
    not from a pod is the failure mode that costs a day, because every symptom it produces
    points somewhere else.
    """
    job_name = f"simpl-dns-check-{abs(hash(hostname)) % 100000}"
    manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": namespace},
        "spec": {
            "ttlSecondsAfterFinished": 120,
            "backoffLimit": 0,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "dns",
                            "image": image,
                            "command": ["sh", "-c", f"nslookup {hostname} || getent hosts {hostname}"],
                        }
                    ],
                }
            },
        },
    }
    await kube.apply(manifest)
    deadline = asyncio.get_event_loop().time() + timeout
    output = ""
    status = "unknown"
    while asyncio.get_event_loop().time() < deadline:
        job = await kube.get("Job", job_name, namespace)
        job_status = job.get("status", {})
        if job_status.get("succeeded"):
            status = "succeeded"
        elif job_status.get("failed"):
            status = "failed"
        if status in ("succeeded", "failed"):
            pods = await kube.list("Pod", namespace, f"job-name={job_name}", limit=5)
            if pods:
                try:
                    output = await kube.logs(namespace, pods[0]["metadata"]["name"], tail_lines=40)
                except UpstreamError:
                    output = ""
            break
        await asyncio.sleep(3)
    try:
        await kube.delete("Job", job_name, namespace, propagation="Background")
    except UpstreamError:
        pass
    addresses = _addresses_from_nslookup(output)
    return {"status": status, "addresses": addresses, "output": output.strip()[:1200]}


IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _strip_log_timestamp(line: str) -> str:
    """Pod logs are read with timestamps, so every line starts with an RFC 3339 stamp.

    Left in place it parses as a getent answer and the caller is told the name resolves to
    "2026-09-19T01:53:13.798Z" — wrong, and wrong in a way that looks like data.
    """
    head, _, rest = line.partition(" ")
    if rest and head[:4].isdigit() and "T" in head and (":" in head or "Z" in head):
        return rest.strip()
    return line


def _addresses_from_nslookup(text: str) -> list[str]:
    """Pull the answered addresses out of nslookup or getent output.

    The first Address line of nslookup names the resolver, not the answer, so lines
    carrying a port (10.3.0.10:53) are skipped.
    """
    addresses = []
    for raw in (text or "").splitlines():
        line = _strip_log_timestamp(raw.strip())
        if line.lower().startswith("address:"):
            candidate = line.split(":", 1)[1].strip()
            if IPV4.match(candidate):  # a resolver line reads "10.3.0.10:53" and is skipped
                addresses.append(candidate)
        elif line and IPV4.match(line.split()[0] if line.split() else ""):
            addresses.append(line.split()[0])
    return sorted(set(addresses))
