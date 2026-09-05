"""AgentCore Observatory — the control-plane resource catalog.

Every one of the 27 ``bedrock-agentcore-control`` resource types is a ROW here,
not a branch in code: the query layer, the HTTP surface and the rail all read
this table, so adding a type is a data change.

**Every verb, response key, parameter and id field in this table was read out of
`aws bedrock-agentcore-control <verb> help`, not inferred.** Two facts make
inference actively wrong, which is why the table is explicit:

* A response key cannot be derived from the type name. All three credential
  provider families answer under ``credentialProviders``, and ``gateways`` and
  ``gateway-targets`` BOTH answer under ``items``.
* 10 of the 27 types are CHILDREN and cannot be listed standalone — they require
  a parent identifier, and ``policy-generation-assets`` requires two. Only the
  17 root types (plus the get-only ``token-vault`` singleton) can be rail items;
  the rest surface inside their parent's detail.

``list_verb`` is empty for a singleton that has no list operation at all.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The two services these resources live on, spelled as the CLI spells them.
#:
#: The split is not cosmetic: the control plane owns the resource inventory,
#: while batch evaluations and A/B tests are DATA-plane operations. A type
#: therefore carries its own service rather than the query layer assuming one,
#: which is what keeps a data-plane type a row here instead of a branch there.
SERVICE_CONTROL = "bedrock-agentcore-control"
SERVICE_DATA = "bedrock-agentcore"

#: CloudWatch Logs. Not an AgentCore service and never a catalog row's service —
#: it is here so every AWS service name this app can reach is declared in one
#: place. Only :mod:`.payload_hints` uses it, and it is why the app's manifest
#: asks for a CloudWatch Logs read permission on top of ``bedrock-agentcore*``.
SERVICE_LOGS = "logs"

#: Rail groups, in the order an operator reads them: what runs, how it is
#: judged, what it remembers, what it can reach, then the supporting planes.
GROUPS: tuple[str, ...] = (
    "compute",
    "evaluation",
    "memory",
    "gateway",
    "tools",
    "identity",
    "policy",
    "config",
    "registry",
    "payment",
)


@dataclass(frozen=True)
class ResourceType:
    """One control-plane resource type.

    ``parent`` names the type whose detail view owns this one; empty means it is
    a root type and therefore a rail item. ``parent_params`` are the CLI flags a
    child needs, paired positionally with ``parent_fields`` — the response fields
    on the ANCESTOR rows that supply their values. ``policy-generation-assets``
    is the case that forces a tuple rather than a single value: it needs both a
    generation id and the engine id above it.
    """

    id: str
    group: str
    list_verb: str
    list_key: str
    get_verb: str
    #: Response field holding this row's own identifier, for building child
    #: queries and detail lookups. Empty when the type is a singleton.
    id_field: str
    parent: str = ""
    parent_params: tuple[str, ...] = ()
    parent_fields: tuple[str, ...] = ()
    #: Which service answers this type. Defaults to the control plane because 27
    #: of the types live there; a data-plane type states it.
    service: str = SERVICE_CONTROL

    @property
    def is_root(self) -> bool:
        return not self.parent

    @property
    def listable(self) -> bool:
        return bool(self.list_verb)


#: The full table. Order within a group is the order the rail renders.
RESOURCE_TYPES: tuple[ResourceType, ...] = (
    # ---- compute -----------------------------------------------------------
    ResourceType(
        id="agent-runtimes",
        group="compute",
        list_verb="list-agent-runtimes",
        list_key="agentRuntimes",
        get_verb="get-agent-runtime",
        id_field="agentRuntimeId",
    ),
    ResourceType(
        id="agent-runtime-versions",
        group="compute",
        list_verb="list-agent-runtime-versions",
        # Versions answer under the same key as the top-level listing: the API
        # returns the same item shape, not a distinct "version" shape.
        list_key="agentRuntimes",
        get_verb="",
        id_field="agentRuntimeVersion",
        parent="agent-runtimes",
        parent_params=("--agent-runtime-id",),
        parent_fields=("agentRuntimeId",),
    ),
    ResourceType(
        id="agent-runtime-endpoints",
        group="compute",
        list_verb="list-agent-runtime-endpoints",
        list_key="runtimeEndpoints",
        get_verb="get-agent-runtime-endpoint",
        id_field="name",
        parent="agent-runtimes",
        parent_params=("--agent-runtime-id",),
        parent_fields=("agentRuntimeId",),
    ),
    ResourceType(
        id="harnesses",
        group="compute",
        list_verb="list-harnesses",
        list_key="harnesses",
        get_verb="get-harness",
        id_field="harnessId",
    ),
    # ---- evaluation --------------------------------------------------------
    ResourceType(
        id="evaluators",
        group="evaluation",
        list_verb="list-evaluators",
        list_key="evaluators",
        get_verb="get-evaluator",
        id_field="evaluatorId",
    ),
    ResourceType(
        id="online-evaluation-configs",
        group="evaluation",
        list_verb="list-online-evaluation-configs",
        list_key="onlineEvaluationConfigs",
        get_verb="get-online-evaluation-config",
        id_field="onlineEvaluationConfigId",
    ),
    # A DATA-plane type, and the only place a completed evaluation's scores are
    # readable through the API at all: online-evaluation results land in
    # CloudWatch, not in a control-plane resource. `list-batch-evaluations` takes
    # no filter parameters, so the listing is account-wide by construction.
    ResourceType(
        id="batch-evaluations",
        group="evaluation",
        list_verb="list-batch-evaluations",
        list_key="batchEvaluations",
        get_verb="get-batch-evaluation",
        id_field="batchEvaluationId",
        service=SERVICE_DATA,
    ),
    # ---- memory ------------------------------------------------------------
    ResourceType(
        id="memories",
        group="memory",
        list_verb="list-memories",
        list_key="memories",
        get_verb="get-memory",
        id_field="id",
    ),
    # ---- gateway -----------------------------------------------------------
    ResourceType(
        id="gateways",
        group="gateway",
        list_verb="list-gateways",
        # Shared with gateway-targets — the reason this table is explicit.
        list_key="items",
        get_verb="get-gateway",
        id_field="gatewayId",
    ),
    ResourceType(
        id="gateway-targets",
        group="gateway",
        list_verb="list-gateway-targets",
        list_key="items",
        get_verb="get-gateway-target",
        id_field="targetId",
        parent="gateways",
        parent_params=("--gateway-identifier",),
        parent_fields=("gatewayId",),
    ),
    ResourceType(
        id="gateway-rules",
        group="gateway",
        list_verb="list-gateway-rules",
        list_key="gatewayRules",
        get_verb="get-gateway-rule",
        id_field="ruleId",
        parent="gateways",
        parent_params=("--gateway-identifier",),
        parent_fields=("gatewayId",),
    ),
    # ---- tools -------------------------------------------------------------
    ResourceType(
        id="browsers",
        group="tools",
        list_verb="list-browsers",
        list_key="browserSummaries",
        get_verb="get-browser",
        id_field="browserId",
    ),
    ResourceType(
        id="browser-profiles",
        group="tools",
        list_verb="list-browser-profiles",
        list_key="profileSummaries",
        get_verb="get-browser-profile",
        id_field="profileId",
    ),
    ResourceType(
        id="code-interpreters",
        group="tools",
        list_verb="list-code-interpreters",
        list_key="codeInterpreterSummaries",
        get_verb="get-code-interpreter",
        id_field="codeInterpreterId",
    ),
    # ---- identity ----------------------------------------------------------
    ResourceType(
        id="workload-identities",
        group="identity",
        list_verb="list-workload-identities",
        list_key="workloadIdentities",
        get_verb="get-workload-identity",
        id_field="name",
    ),
    ResourceType(
        id="api-key-credential-providers",
        group="identity",
        list_verb="list-api-key-credential-providers",
        list_key="credentialProviders",
        get_verb="get-api-key-credential-provider",
        id_field="name",
    ),
    ResourceType(
        id="oauth2-credential-providers",
        group="identity",
        list_verb="list-oauth2-credential-providers",
        list_key="credentialProviders",
        get_verb="get-oauth2-credential-provider",
        id_field="name",
    ),
    ResourceType(
        # A singleton: `get-token-vault` takes no arguments and there is no list
        # operation, so it is a rail item that renders one object, not a list.
        id="token-vault",
        group="identity",
        list_verb="",
        list_key="",
        get_verb="get-token-vault",
        id_field="",
    ),
    # ---- policy ------------------------------------------------------------
    ResourceType(
        id="policy-engines",
        group="policy",
        list_verb="list-policy-engines",
        list_key="policyEngines",
        get_verb="get-policy-engine",
        id_field="policyEngineId",
    ),
    ResourceType(
        id="policies",
        group="policy",
        list_verb="list-policies",
        list_key="policies",
        get_verb="get-policy",
        id_field="policyId",
        parent="policy-engines",
        parent_params=("--policy-engine-id",),
        parent_fields=("policyEngineId",),
    ),
    ResourceType(
        id="policy-generations",
        group="policy",
        list_verb="list-policy-generations",
        list_key="policyGenerations",
        get_verb="get-policy-generation",
        id_field="policyGenerationId",
        parent="policy-engines",
        parent_params=("--policy-engine-id",),
        parent_fields=("policyEngineId",),
    ),
    ResourceType(
        # The only two-parent type: it needs the generation id AND the engine id
        # from the row above it. This is why parent_params is a tuple.
        id="policy-generation-assets",
        group="policy",
        list_verb="list-policy-generation-assets",
        list_key="policyGenerationAssets",
        get_verb="",
        id_field="",
        parent="policy-generations",
        parent_params=("--policy-generation-id", "--policy-engine-id"),
        parent_fields=("policyGenerationId", "policyEngineId"),
    ),
    # ---- config ------------------------------------------------------------
    ResourceType(
        id="configuration-bundles",
        group="config",
        list_verb="list-configuration-bundles",
        list_key="bundles",
        get_verb="get-configuration-bundle",
        id_field="bundleId",
    ),
    ResourceType(
        id="configuration-bundle-versions",
        group="config",
        list_verb="list-configuration-bundle-versions",
        list_key="versions",
        get_verb="get-configuration-bundle-version",
        id_field="versionId",
        parent="configuration-bundles",
        parent_params=("--bundle-id",),
        parent_fields=("bundleId",),
    ),
    # ---- registry ----------------------------------------------------------
    ResourceType(
        id="registries",
        group="registry",
        list_verb="list-registries",
        list_key="registries",
        get_verb="get-registry",
        id_field="registryId",
    ),
    ResourceType(
        id="registry-records",
        group="registry",
        list_verb="list-registry-records",
        list_key="registryRecords",
        get_verb="get-registry-record",
        id_field="recordId",
        parent="registries",
        parent_params=("--registry-id",),
        parent_fields=("registryId",),
    ),
    # ---- payment -----------------------------------------------------------
    ResourceType(
        id="payment-managers",
        group="payment",
        list_verb="list-payment-managers",
        list_key="paymentManagers",
        get_verb="get-payment-manager",
        id_field="paymentManagerId",
    ),
    ResourceType(
        id="payment-connectors",
        group="payment",
        list_verb="list-payment-connectors",
        list_key="paymentConnectors",
        get_verb="get-payment-connector",
        id_field="paymentConnectorId",
        parent="payment-managers",
        parent_params=("--payment-manager-id",),
        parent_fields=("paymentManagerId",),
    ),
    ResourceType(
        id="payment-credential-providers",
        group="payment",
        list_verb="list-payment-credential-providers",
        list_key="credentialProviders",
        get_verb="get-payment-credential-provider",
        id_field="name",
    ),
)

_BY_ID: dict[str, ResourceType] = {t.id: t for t in RESOURCE_TYPES}


def by_id(type_id: str) -> ResourceType | None:
    """The type with this id, or None. Never raises on unknown input."""
    return _BY_ID.get(type_id)


def root_types() -> tuple[ResourceType, ...]:
    """Types that can be opened directly from the rail."""
    return tuple(t for t in RESOURCE_TYPES if t.is_root)


def children_of(type_id: str) -> tuple[ResourceType, ...]:
    """Types whose rows live inside ``type_id``'s detail view."""
    return tuple(t for t in RESOURCE_TYPES if t.parent == type_id)
