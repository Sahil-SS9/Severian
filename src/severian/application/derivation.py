from __future__ import annotations

from typing import Any

from severian.application.ports import CanonicalRepository, DerivationGateway
from severian.domain.models import Memory, Scope


class CloudPolicyError(PermissionError):
    pass


class DerivationService:
    def __init__(self, repository: CanonicalRepository):
        self.repository = repository

    def derive_observation(
        self,
        scope: Scope,
        observation_id: str,
        gateway: DerivationGateway,
        *,
        policy: dict[str, object],
    ) -> list[Memory]:
        observation = self.repository.get_observation(scope, observation_id)
        if getattr(gateway, "is_cloud", False) and policy.get("cloud_allowed") is not True:
            raise CloudPolicyError("cloud derivation is forbidden by collection policy")

        candidates = gateway.derive(observation.content, policy=policy)
        memories: list[Memory] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            kind = str(candidate.get("kind", "fact"))
            content = str(candidate.get("content", "")).strip()
            if not content:
                continue
            identity = (kind, content)
            if identity in seen:
                continue
            seen.add(identity)
            memories.append(
                self.repository.add_memory(
                    Memory(
                        scope=scope,
                        kind=kind,
                        content=content,
                        source_observation_ids=[observation.id],
                        model_id=str(candidate.get("model_id", getattr(gateway, "model_id", "unknown"))),
                        prompt_version=str(
                            candidate.get("prompt_version", getattr(gateway, "prompt_version", "unknown"))
                        ),
                        confidence=float(candidate.get("confidence", 0.5)),
                    )
                )
            )
        return memories
