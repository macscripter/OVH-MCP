"""Initialising a Governance Authority's tier-1 trust chain.

A freshly deployed authority has no trust material, and three of its components —
tier2-gateway, tier2-proxy and users-roles — never become healthy until it does. The
programme documents the fix as six HTTP calls made against two services after a
`kubectl port-forward`.

Port-forwarding is not available to a server that runs somewhere else entirely, so the
same six calls go through the Kubernetes API server's service proxy. The sequence is
identical; only the transport differs.

If the agent was deployed with `development.enableAutomaticInitialization: true`, a job in
the cluster has already done this. `status()` below detects that and the workflow stops
rather than creating a second authority participant.
"""

from __future__ import annotations

import json
from typing import Any

from ..errors import UpstreamError
from ..kube.client import KubeClient

IDENTITY_SERVICE = "identity-provider"
AUTH_SERVICE = "authentication-provider"
SERVICE_PORT = 8080

# These are Spring Boot services, so health lives under /actuator. The Quarkus path is
# tried too because other components in the platform use it, and the participants endpoint
# is the last resort: any HTTP answer at all proves the service is up.
HEALTH_PATHS = ("/actuator/health", "/q/health/ready", "/tier1/v2/participants")


class AuthorityInitialiser:
    def __init__(
        self,
        kube: KubeClient,
        namespace: str,
        identity_service: str = IDENTITY_SERVICE,
        auth_service: str = AUTH_SERVICE,
        port: int = SERVICE_PORT,
    ) -> None:
        self.kube = kube
        self.namespace = namespace
        self.identity_service = identity_service
        self.auth_service = auth_service
        self.port = port

    async def _call(
        self, service: str, method: str, path: str, body: Any | None = None
    ) -> tuple[int, Any]:
        status, text = await self.kube.service_proxy(
            self.namespace, service, self.port, path, method=method, json_body=body
        )
        try:
            payload: Any = json.loads(text) if text else None
        except json.JSONDecodeError:
            payload = text
        return status, payload

    async def reachable(self) -> dict[str, Any]:
        """Are both services answering? Anything else is a waste of six calls."""
        out: dict[str, Any] = {}
        for label, service in (
            ("identity_provider", self.identity_service),
            ("authentication_provider", self.auth_service),
        ):
            entry: dict[str, Any] = {"service": service, "reachable": False}
            for path in HEALTH_PATHS:
                try:
                    status, payload = await self._call(service, "GET", path)
                except UpstreamError as exc:
                    entry["error"] = str(exc)
                    continue
                if status == 404:
                    continue
                entry.update(
                    {
                        "reachable": status < 500,
                        "status": status,
                        "probed": path,
                        "detail": _clip(payload, 200),
                    }
                )
                break
            out[label] = entry
        return out

    async def status(self) -> dict[str, Any]:
        """Does this authority already have a participant and credentials?"""
        try:
            status, participants = await self._call(
                self.identity_service, "GET", "/tier1/v2/participants"
            )
        except UpstreamError as exc:
            return {"initialised": None, "error": str(exc)}
        if status >= 400:
            return {"initialised": None, "http_status": status, "detail": _clip(participants)}
        items = _items_of(participants)
        authorities = [
            p
            for p in (items or [])
            if isinstance(p, dict)
            and (p.get("isAuthority") or p.get("participantType") == "GOVERNANCE_AUTHORITY")
        ]
        return {
            "initialised": bool(authorities),
            "participant_count": len(items or []),
            "authority_participants": [
                {"id": p.get("id"), "organization": p.get("organization")} for p in authorities
            ],
        }

    async def run(
        self,
        organization: str,
        common_name: str,
        country: str,
        organizational_unit: str,
        keypair_name: str = "initialization-authority",
        reason: str = "initialization-authority",
    ) -> dict[str, Any]:
        """The six calls, in order, each one reported whether it succeeds or not."""
        steps: list[dict[str, Any]] = []

        def record(step: str, status: int, detail: Any = None) -> None:
            steps.append({"step": step, "http_status": status, "detail": _clip(detail)})

        # 1. A keypair in the authentication provider.
        status, payload = await self._call(
            self.auth_service, "POST", "/tier1/v2/keypairs", {"name": keypair_name}
        )
        record("create_keypair", status, payload)
        if status >= 400:
            return _failed(steps, "the authentication provider refused to create a keypair")
        keypair_id = _id_of(payload)
        if not keypair_id:
            return _failed(steps, "the keypair response carried no id")

        # 2. A certificate signing request for that keypair.
        status, csr = await self._call(
            self.auth_service,
            "POST",
            f"/tier1/v2/keypairs/{keypair_id}/csr",
            {
                "commonName": common_name,
                "country": country,
                "organization": organization,
                "organizationalUnit": organizational_unit,
            },
        )
        record("create_csr", status, "«csr»" if status < 400 else csr)
        if status >= 400:
            return _failed(steps, "the CSR could not be generated")

        # 3. The participant, in the identity provider.
        status, participant = await self._call(
            self.identity_service,
            "POST",
            "/tier1/v2/participants",
            {
                "organization": organization,
                "participantType": "GOVERNANCE_AUTHORITY",
                "isAuthority": True,
            },
        )
        record("create_participant", status, participant)
        if status >= 400:
            return _failed(steps, "the participant could not be created")
        participant_id = _id_of(participant)
        if not participant_id:
            return _failed(steps, "the participant response carried no id")

        # 4. Hand the CSR to the participant. The whole response of step 2 is the body.
        status, payload = await self._call(
            self.identity_service,
            "PUT",
            f"/tier1/v2/participants/{participant_id}/csr",
            csr,
        )
        record("upload_csr", status, payload)
        if status >= 400:
            return _failed(steps, "the CSR was rejected by the identity provider")

        # 5. Issue the credentials — this is the signed chain.
        status, credentials = await self._call(
            self.identity_service,
            "POST",
            f"/tier1/v2/participants/{participant_id}/credentials",
            {"reason": reason},
        )
        record("issue_credentials", status, "«certificate chain»" if status < 400 else credentials)
        if status >= 400:
            return _failed(steps, "credentials could not be issued")
        content = credentials.get("content") if isinstance(credentials, dict) else None
        if not content:
            return _failed(steps, "the credentials response carried no 'content' field")

        # 6. Store the chain back in the authentication provider.
        status, payload = await self._call(
            self.auth_service,
            "POST",
            "/tier1/v2/credentials",
            {"reason": reason, "content": content},
        )
        record("store_credentials", status, payload)
        if status >= 400:
            return _failed(steps, "the authentication provider refused the credentials")

        return {
            "initialised": True,
            "participant_id": participant_id,
            "keypair_id": keypair_id,
            "steps": steps,
            "next": "tier2-gateway, tier2-proxy and users-roles should become healthy within a "
            "few minutes. Check with k8s_pod_health.",
        }


def _failed(steps: list[dict[str, Any]], message: str) -> dict[str, Any]:
    return {
        "initialised": False,
        "failed_at": steps[-1]["step"] if steps else None,
        "message": message,
        "steps": steps,
        "hint": "Check that the agent is deployed and its pods are running "
        "(k8s_pod_health), and that the identity and authentication providers answer "
        "(the reachability block of this tool's output).",
    }


def _items_of(payload: Any) -> list[Any]:
    """The participants endpoint answers a page: {self, page, pageSize, total, items[]}.

    Other services in the platform page with `content`, so both shapes are accepted, as is
    a bare list.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "content", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _id_of(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("id", "keypairId", "participantId", "uuid"):
            if payload.get(key):
                return str(payload[key])
    if isinstance(payload, str) and payload.strip():
        return payload.strip().strip('"')
    return None


def _clip(value: Any, limit: int = 600) -> Any:
    if isinstance(value, str):
        return value[:limit] + ("…" if len(value) > limit else "")
    if isinstance(value, (dict, list)):
        text = json.dumps(value, default=str)
        return json.loads(text) if len(text) <= limit else text[:limit] + "…"
    return value
