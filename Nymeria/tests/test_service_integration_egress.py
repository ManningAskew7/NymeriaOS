"""Tool requests are screened where they LEAVE, not only where they parse
(E10-02 slice C).

``service_integration_base.base_url()`` runs every vault-supplied address through
``validate_http_egress_url(..., resolve_dns=False)``. That is a parse-shaped
screen: it rejects a literal private, loopback or metadata IP, and it lets a
HOSTNAME through no matter where that hostname resolves. The request was then
issued on a bare ``httpx.Client``, so nothing between the screen and the socket
looked at the concrete target. Measured on the reference host before this
change: ``http://localhost:8000/x`` ALLOW, ``http://nymeria-postgres:5432/``
ALLOW, ``http://127.0.0.1:8000/x`` BLOCK.

Three controls answer that, and each has a gate here because each is the kind of
thing a later change removes without noticing. A fourth, added later for a
different defect in the same family, is described with its own gate at the end of
this file (``require_joined_destination``, E10-02-D: the address may come from a
vault record only if the secret riding to it came from the same record).

* ``request_with_policy`` (32 modules, 36 sites) re-evaluates the concrete
  target with DNS resolution ON and holds ``pinned_dns_resolution`` across the
  send. The pin is not a bonus: without it the check and the connect perform
  independent lookups and a short-TTL name can differ between them.
* ``policy_http_client`` builds every httpx client in ``nymeria/tools/``. Its
  only job is neutralising the ``HTTP_PROXY``/``HTTPS_PROXY`` mounts, because a
  proxied request never reaches the address the policy approved: the socket goes
  to the proxy and the PROXY resolves the name, which makes both the pin and the
  private-address block advisory.
* ``signed_endpoint_url`` covers the two boto3 endpoint overrides, which reach
  neither of the above because botocore has its own HTTP stack.

The gates are scoped to ALL of ``nymeria/tools/``, not to the
``*_service_integrations.py`` glob. The first version of this file used the
glob, which silently excluded ``developer_platform_integrations.py`` (the module
whose second request path this slice fixed) and ``web_search_integrations.py``
(which aims a client at a vault-supplied address). A gate that does not cover
the module the change is about is worse than no gate, because it reports
success.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import socket

import httpx
import pytest
from cryptography.fernet import Fernet

from nymeria.core import http_policy
from nymeria.core.accounts import AccountsRepo
from nymeria.core.credential_vault import CredentialVaultRepo
from nymeria.tools import service_integration_base as base

TOOLS_PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "nymeria" / "tools"

# Modules that call a method on a policy-built client directly instead of going
# through ``request_with_policy``, each with the reason it is allowed to. This is
# the declared half of the register: a NEW module doing this fails the gate until
# someone writes down why, which is the only thing standing between a considered
# carve-out and an unnoticed one.
_DIRECT_CLIENT_CALLS = {
    "web.py": "one hard-coded vendor host (api.perplexity.ai); no caller-supplied address",
    "think.py": "one hard-coded vendor host (openrouter.ai); no caller-supplied address",
    "image_gen_integrations.py": (
        "three module-constant vendor hosts. The BFL poll target comes from the "
        "vendor's own response body, which is a vendor-controlled address rather "
        "than a caller-controlled one, and the credential is already going there"
    ),
    "web_search_integrations.py": (
        "four module-constant vendor hosts, plus SearXNG, whose address is screened "
        "one level up in _get_searxng_base_url because only that function can tell a "
        "vault-supplied address from the operator's sidecar setting"
    ),
    "outlook_email.py": (
        "two module-constant Microsoft hosts (MICROSOFT_TOKEN_URI, GRAPH_BASE). The "
        "Graph request builds its URL from a module constant plus a tool-chosen "
        "path segment, so the HOST is never caller-supplied"
    ),
    "auth_cache_utils.py": (
        "one hard-coded Google userinfo host, plus the OAuth token endpoint, which "
        "slice A made registry-derived (exchange_code_for_tokens' token_uri "
        "parameter has no production caller and defaults to the registry)"
    ),
    "claude_code_bridge.py": (
        "the runner service's operator-configured base_url, which is LOOPBACK by "
        "design (the bridge drives Claude Code on the host). Screening it would "
        "refuse the only address it is ever meant to have; same provenance rule as "
        "the SearXNG sidecar and the S3_ENDPOINT_URL settings leg"
    ),
}
# `http_api.py` and `public_info_integrations.py` are deliberately absent: they
# build policy clients and hand them to `httpx_request_with_policy` themselves
# (they need its redirect chain and decision for their audit log, which
# `request_with_policy` drops). The staleness assertion below rejected them when
# they were listed here out of caution, which is the register working.

# Modules building a boto3 client, mapped to the settings attribute their
# endpoint override falls back to (None where there is no settings leg). The
# value is ASSERTED below, not decoration: an entry that says "no settings leg"
# while the module has one would hide exactly the case the exemption turns on.
_BOTO_ENDPOINT_SITES = {
    "aws_service_integrations.py": None,
    "file_storage_service_integrations.py": "s3_endpoint_url",
}


class _FakeResponse:
    is_redirect = False
    status_code = 200

    def __init__(self):
        self.headers = {}


class _RecordingClient:
    """Stands in for ``httpx.Client``, recording what the wrapper forwards."""

    def __init__(self, on_request=None):
        self.calls = []
        self._on_request = on_request

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self._on_request is not None:
            self._on_request(method, url, kwargs)
        return _FakeResponse()


def _public(*ips):
    """A resolver stub, so no test in this file needs live DNS."""
    return lambda host, port: list(ips)


def _tools_modules():
    return sorted(TOOLS_PACKAGE.glob("*.py"))


def _factory_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to ``policy_http_client``, however it was imported.

    Every module today spells it ``as _http_client``, but keying the gate on
    that literal would let a module import it under any other name and leave the
    gate silently blind, which is the same class of miss as matching only
    ``httpx.Client(``.
    """
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases.update(
                (a.asname or a.name)
                for a in node.names
                if a.name == "policy_http_client"
            )
    return aliases


def _bound_names(node, aliases: set[str]) -> set[str]:
    """Names this statement binds to a policy client, across target shapes.

    Covers plain, annotated (``client: httpx.Client = _http_client()``) and
    tuple assignment. Attribute targets (``self._client = _http_client()``) are
    reported by the caller as an immediate offence instead: the receiver escapes
    the function scope entirely, so no per-function gate can follow it.
    """
    def _is_factory(value) -> bool:
        return (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in aliases
        )

    names: set[str] = set()
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if _is_factory(node.value) and isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                if _is_factory(node.value):
                    # One client unpacked across several names: any of them may
                    # be it, so treat them all as bound.
                    names.update(e.id for e in target.elts if isinstance(e, ast.Name))
                elif isinstance(node.value, (ast.Tuple, ast.List)):
                    # Positional unpacking, so only the matching element counts.
                    names.update(
                        t.id
                        for t, v in zip(target.elts, node.value.elts)
                        if isinstance(t, ast.Name) and _is_factory(v)
                    )
    elif isinstance(node, ast.AnnAssign) and _is_factory(node.value):
        if isinstance(node.target, ast.Name):
            names.add(node.target.id)
    elif isinstance(node, ast.With):
        for item in node.items:
            if _is_factory(item.context_expr) and isinstance(item.optional_vars, ast.Name):
                names.add(item.optional_vars.id)
    return names


def _escaping_client_bindings(tree: ast.AST, aliases: set[str]) -> list[str]:
    """Policy clients bound somewhere a per-function gate cannot follow."""
    escapes: list[str] = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in aliases
        ):
            continue
        if any(isinstance(t, (ast.Attribute, ast.Subscript)) for t in targets):
            escapes.append(f"{node.lineno} client stored on an attribute")
    return escapes


def _direct_client_calls(tree: ast.AST) -> list[str]:
    """Receiver calls on a name bound to ``policy_http_client(...)``, per function.

    Scoping is the whole difficulty here, and getting it wrong has produced a
    false result in this gate three times: once by matching the bare name
    ``client`` (which is also the boto3 handle in two modules), once by seeing
    only ``with`` bindings and missing plain assignment, and once by collecting
    binding names MODULE-wide, so an httpx ``client`` in one function condemned a
    boto3 ``client`` in another. Bindings are therefore resolved inside each
    function scope and never leak out of it.

    The binding shapes are deliberately broader than what the corpus uses today.
    The realistic regression is not someone inventing a new pattern, it is
    someone adding a type annotation to one of the 38 existing assignments and
    silently leaving the gate's view.
    """
    aliases = _factory_aliases(tree)
    if not aliases:
        return []
    hits: list[str] = _escaping_client_bindings(tree, aliases)
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names: set[str] = set()
        for node in ast.walk(scope):
            names |= _bound_names(node, aliases)
        if not names:
            continue
        hits.extend(
            f"{node.lineno} {node.func.value.id}.{node.func.attr}("
            for node in ast.walk(scope)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in names
        )
    return hits


# --- the corpus gates ------------------------------------------------------


# Every way httpx hands out a client that did not come from the policy factory.
# The one-shot module functions matter as much as the constructors: `httpx.get`
# builds a throwaway `Client(trust_env=True)` internally, so it inherits the env
# proxy mounts exactly like a bare constructor would, while looking like nothing
# at all at the call site.
_HTTPX_CLIENT_ATTRS = frozenset(
    {"Client", "AsyncClient", "get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}
)


def _httpx_module_aliases(tree: ast.AST) -> set[str]:
    """Names bound to the httpx MODULE, however it was imported."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(a.asname or a.name for a in node.names if a.name == "httpx")
    return aliases


def _direct_httpx_names(tree: ast.AST) -> set[str]:
    """Client-yielding names imported FROM httpx (``from httpx import Client``)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "httpx":
            names.update(
                (a.asname or a.name) for a in node.names if a.name in _HTTPX_CLIENT_ATTRS
            )
    return names


def test_no_tool_module_builds_a_bare_httpx_client():
    """Every client in ``nymeria/tools/`` comes from the policy factory.

    No exemptions, deliberately. The factory's whole job is to drop the env
    proxy mounts, and a module that opts out gets a client whose requests the
    policy evaluates and then does not govern: the pin is keyed to the target
    host while the socket connects to the proxy.

    This deliberately looks past ``httpx.Client(``. Matching only that spelling
    left nine live module-level ``httpx.post``/``httpx.get`` calls invisible
    while the docstring claimed there were no exemptions, so the alias forms
    (``import httpx as hx``, ``from httpx import Client``), the async client and
    the one-shot module functions are all in scope here.
    """
    offenders = []
    for path in _tools_modules():
        tree = ast.parse(path.read_text())
        module_aliases = _httpx_module_aliases(tree)
        direct_names = _direct_httpx_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _HTTPX_CLIENT_ATTRS
                and isinstance(func.value, ast.Name)
                and func.value.id in module_aliases
            ):
                offenders.append(f"{path.name}:{node.lineno} {func.value.id}.{func.attr}")
            elif isinstance(func, ast.Name) and func.id in direct_names:
                offenders.append(f"{path.name}:{node.lineno} {func.id}")
    assert offenders == [], (
        "these reach httpx without the policy factory, so they inherit the env "
        "proxy mounts; use core.http_policy.policy_http_client: " + ", ".join(offenders)
    )


def test_no_tool_module_calls_a_policy_client_directly():
    """Every request on a policy-built client goes through a policy-aware helper.

    ``request_with_policy(client, ...)`` takes the client as an ARGUMENT, so a
    receiver call (``client.get(...)``) is exactly the shape that skips the
    egress-time check. Scoped to the client binding rather than the name
    ``client``, because that name also holds the boto3 handle in two modules and
    a gate that has to be relaxed to pass stops gating.
    """
    offenders = {}
    for path in _tools_modules():
        hits = _direct_client_calls(ast.parse(path.read_text()))
        if hits:
            offenders[path.name] = hits
    undeclared = {k: v for k, v in offenders.items() if k not in _DIRECT_CLIENT_CALLS}
    assert undeclared == {}, (
        "these reach the network without the egress-time check; route them through "
        f"service_integration_base.request_with_policy or declare why not: {undeclared}"
    )
    stale = set(_DIRECT_CLIENT_CALLS) - set(offenders)
    assert stale == set(), f"declared carve-outs that no longer apply: {stale}"


def test_the_wrapper_covers_the_corpus_it_claims_to():
    """Pins the counts the docstrings and SECURITY.md quote.

    The first version said 31 modules and 34 sites, which was the
    ``*_service_integrations.py`` glob with the two ``developer_platform`` sites
    missing. A count in prose that nothing checks is how the audit's own
    documents drifted.
    """
    modules, sites = set(), 0
    for path in _tools_modules():
        if path.name == "http_api.py":
            continue  # defines its OWN _request_with_policy, a different function
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "_request_with_policy":
                    modules.add(path.name)
                    sites += 1
    # A ratchet, not an equality: the house idiom (test_resource_layout ratchets
    # the uncontrolled set, the credential gate fails below 500). Exact equality
    # would fail a SECURITY test the day someone adds a legitimate integration,
    # which teaches people to edit the number rather than read it.
    assert (len(modules), sites) >= (32, 36), (
        f"coverage went BACKWARDS: {len(modules)} modules / {sites} sites, was 32 / 36"
    )


def test_every_boto3_endpoint_override_goes_through_the_signed_endpoint_helper():
    """The gate for the two addresses no HTTP-layer control can see.

    botocore does not pass through ``base_url()`` or ``request_with_policy``, so
    a boto3 module that resolves ``endpoint_url`` itself has NO screen at all.
    This is what makes that visible when a third one appears.
    """
    for path in _tools_modules():
        src = path.read_text()
        if "boto3.client(" not in src:
            continue
        assert path.name in _BOTO_ENDPOINT_SITES, (
            f"{path.name} builds a boto3 client; if it resolves an endpoint override it "
            "must use service_integration_base.signed_endpoint_url, then be declared here"
        )
        assert "signed_endpoint_url(" in src, f"{path.name} resolves an endpoint unscreened"
        settings_attr = _BOTO_ENDPOINT_SITES[path.name]
        if settings_attr is None:
            assert "from_settings=" not in src, (
                f"{path.name} is declared as having no settings leg but passes one"
            )
        else:
            assert f'from_settings=_settings_value("{settings_attr}")' in src, (
                f"{path.name} is declared as falling back to {settings_attr}; it does not"
            )
    for name in _BOTO_ENDPOINT_SITES:
        assert "boto3.client(" in (TOOLS_PACKAGE / name).read_text(), f"stale entry: {name}"


def test_the_signing_join_is_computed_at_every_boto3_site():
    """``keys_from_vault=True`` hard-coded would satisfy the helper and mean nothing.

    The helper cannot check its own precondition, so the gate checks the shape
    of the argument instead: it must be an expression over the vault lookups,
    never a literal.
    """
    for name in _BOTO_ENDPOINT_SITES:
        tree = ast.parse((TOOLS_PACKAGE / name).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "signed_endpoint_url"
            ):
                arg = next(k.value for k in node.keywords if k.arg == "keys_from_vault")
                assert not isinstance(arg, ast.Constant), (
                    f"{name} hard-codes keys_from_vault; it must be computed from the "
                    "vault lookups or the join is decorative"
                )


# --- the hostname bypass ---------------------------------------------------


def test_a_hostname_resolving_into_private_space_is_refused_at_egress():
    """The finding itself, with the resolver injected so it pins the real rule.

    An earlier version used ``nymeria-postgres``, which on this host simply does
    not resolve: it passed on ``dns_resolution_failed`` and would have passed
    with the DNS check switched off entirely. The point is a name that resolves
    FINE and resolves somewhere private, which is what the parse screen cannot
    see and the egress check can.
    """
    url = "http://build-server.corp/x"
    assert base.base_url(url) == url  # the parse screen has no objection

    client = _RecordingClient()
    with pytest.raises(RuntimeError) as excinfo:
        base.request_with_policy(client, "GET", url, resolver=_public("10.0.0.5"))
    assert "private_network" in str(excinfo.value)
    assert client.calls == [], "the request was issued before the policy refused it"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://localhost:8000/x", "loopback_network"),
        ("http://metadata.google.internal/", "metadata_target"),
        ("http://127.0.0.1:8000/x", "loopback_network"),
    ],
)
def test_the_other_refusal_reasons_reach_the_caller_intact(url, reason):
    with pytest.raises(RuntimeError, match=reason):
        base.request_with_policy(_RecordingClient(), "GET", url)


def test_a_public_target_still_goes_through_untouched():
    """The control costs no capability on the path everything actually uses."""
    client = _RecordingClient()
    response = base.request_with_policy(
        client,
        "POST",
        "http://vendor.example/v1/things",
        params={"a": 1},
        json={"b": 2},
        headers={"Authorization": "Bearer t"},
        resolver=_public("93.184.216.34"),
    )
    assert isinstance(response, _FakeResponse)
    method, url, kwargs = client.calls[0]
    assert (method, url) == ("POST", "http://vendor.example/v1/things")
    assert kwargs["params"] == {"a": 1}
    assert kwargs["json"] == {"b": 2}
    assert kwargs["headers"] == {"Authorization": "Bearer t"}


def test_a_redirect_is_returned_rather_than_chased():
    """Redirect handling must stay exactly what bare ``httpx.Client`` did.

    ``httpx_request_with_policy`` defaults to FOLLOWING redirects and the modules
    it now backs did not. Chasing a 3xx would re-aim a request that already
    carries the credential, so ``follow_redirects=False`` is a
    behaviour-preservation clause and a control at the same time.
    """

    class _Redirecting(_RecordingClient):
        def request(self, method, url, **kwargs):
            super().request(method, url, **kwargs)
            response = _FakeResponse()
            response.is_redirect = True
            response.status_code = 302
            response.headers = {"location": "http://127.0.0.1:8000/internal"}
            response.url = url
            return response

    client = _Redirecting()
    response = base.request_with_policy(
        client, "GET", "http://vendor.example/a", resolver=_public("93.184.216.34")
    )
    assert response.status_code == 302
    assert len(client.calls) == 1, "the redirect was followed"


def test_the_send_happens_inside_the_dns_pin(monkeypatch):
    """The pin is live for the duration of the request, not just the check.

    Uses a name real DNS cannot answer, so the only way ``getaddrinfo`` can
    succeed inside the send is the pin installed from the policy decision. Drop
    ``pinned_dns_resolution`` from the wrapper and this raises ``gaierror``,
    which is precisely the window a rebinding attacker needs.
    """
    monkeypatch.setattr(http_policy, "_resolve_host", lambda host, port: ("93.184.216.34",))
    seen = {}

    def probe(method, url, kwargs):
        seen["addrs"] = [info[4][0] for info in socket.getaddrinfo("pinned.invalid", 443)]

    base.request_with_policy(_RecordingClient(probe), "GET", "https://pinned.invalid/x")
    assert seen["addrs"] == ["93.184.216.34"]


def test_the_error_names_what_was_refused():
    """The translation exists for the message, not for the exception type.

    ``HTTPPolicyViolation`` already subclasses ``RuntimeError``, so which
    ``except`` catches it never changed. What changed is that the agent is told
    the request was blocked by policy and why, instead of getting a bare target.
    """
    with pytest.raises(RuntimeError) as excinfo:
        base.request_with_policy(_RecordingClient(), "GET", "http://127.0.0.1/x")
    message = str(excinfo.value)
    assert "blocked by egress policy" in message
    assert "loopback_network" in message
    assert "http://127.0.0.1/x" in message


# --- the proxy the policy cannot see through -------------------------------


def test_the_policy_client_ignores_env_proxies(monkeypatch):
    """A proxied request never reaches the address the policy approved.

    The socket connects to the proxy and the PROXY resolves the name, so both
    the pin and the private-address block become advisory. Asserted against a
    bare client in the same test so the contrast is the assertion: without the
    factory this is simply how every integration behaves.
    """
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")

    for url in ("http://vendor.example/x", "https://vendor.example/x"):
        target = httpx.URL(url)
        with httpx.Client(timeout=1.0) as bare:
            assert bare._transport_for_url(target) is not bare._transport, (
                "premise broken: a bare client no longer mounts env proxies"
            )
        with http_policy.policy_http_client(timeout=1.0) as guarded:
            assert guarded._transport_for_url(target) is guarded._transport


def test_the_policy_client_keeps_trust_env_for_everything_else():
    """Only proxy routing is dropped.

    ``trust_env=False`` would have been one word shorter and would also have
    disabled ``SSL_CERT_FILE`` and ``.netrc``, which the deployment most likely
    to set a proxy is the most likely to need.
    """
    with http_policy.policy_http_client(timeout=1.0) as client:
        assert client.trust_env is True


def test_an_explicit_mount_still_wins():
    """The factory sets defaults, it does not overrule a caller that means it."""
    sentinel = httpx.HTTPTransport()
    with http_policy.policy_http_client(timeout=1.0, mounts={"http://": sentinel}) as client:
        assert client._transport_for_url(httpx.URL("http://vendor.example/x")) is sentinel


# --- the second request path the audit appendix missed ---------------------


def test_the_graphql_body_path_is_screened_too(monkeypatch):
    """``_request_json_body`` was the uncovered sibling of a covered helper.

    ``developer_platform_integrations`` was the one module the audit appendix
    treated as policy-covered, because its ``_request_json`` genuinely was.
    ``graphql_execute_query`` calls ``_request_json_body``, which was not. Driven
    through the agent-facing tool rather than the helper, because ``endpoint`` is
    a plain tool argument: no vault record is needed to aim this one.
    """
    from nymeria.tools import developer_platform_integrations as tools

    monkeypatch.setattr(tools, "_graphql_headers", lambda **kwargs: {})
    out = tools.graphql_execute_query.func(
        query="{ me { id } }", endpoint="http://localhost:9000/graphql"
    )
    assert "blocked by egress policy" in out


# --- the vault fixture shared by the credential-driven tests ---------------


@pytest.fixture
def vault_setup(tmp_path, monkeypatch):
    """Isolated vault + settings sandbox (mirrors the slice B gate fixture)."""
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())

    from nymeria.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("NYMERIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")

    db_path = tmp_path / "accounts.db"
    accounts = AccountsRepo(db_path)
    accounts.create_user("alice", "alice@example.com", "Alice")

    from nymeria.core import credential_vault as vault_module

    monkeypatch.setattr(vault_module, "_repo_instance", None, raising=False)
    repo = CredentialVaultRepo(db_path)
    monkeypatch.setattr(vault_module, "get_credential_vault_repo", lambda db_path=None: repo)

    yield repo

    settings_module.get_settings.cache_clear()


def _mint(repo, *, name, provider, secret_fields):
    return repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name=name,
        provider=provider,
        kind="api_key",
        account_label=name,
        metadata={},
        scopes=[],
        allowed_targets=["native_tool:*"],
        secret_fields=secret_fields,
        created_by_user_id="alice",
    )


# --- searxng: the one provider whose credential IS an address --------------


def test_a_planted_searxng_address_is_screened(vault_setup, monkeypatch):
    """Slice B cannot help here, so the screen has to.

    SearXNG needs no API key, so its record holds an address and nothing else.
    With no secret to anchor against, the slice B join has nothing to check and
    declines to act. Nothing rides the request, so no credential leaks, but the
    response comes back into the transcript, which makes it a read-capable probe
    of whatever the deployment can reach.

    The planted address must be made INERT, not fatal. Any identified caller can
    POST this record, so a raise here would let one of them break search for
    every user of the deployment; the operator's own setting has to keep working
    underneath it. That is the pairing this test and the next one assert.
    """
    from nymeria.tools import web_search_integrations as tools

    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng.example.com:8080")
    from nymeria.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    _mint(vault_setup, name="planted", provider="searxng", secret_fields={"base_url": "http://localhost:8000"})

    resolved = tools._get_searxng_base_url({"configurable": {"user_id": "alice"}})

    assert resolved == "http://searxng.example.com:8080"
    assert "localhost" not in (resolved or "")


def test_the_searxng_sidecar_setting_is_left_alone(vault_setup, monkeypatch):
    """``SEARXNG_BASE_URL=http://searxng:8080`` is the documented deployment.

    It is also an internal address, which is the whole reason the screen keys on
    provenance rather than on the address.
    """
    from nymeria.tools import web_search_integrations as tools

    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng:8080")
    from nymeria.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    assert tools._get_searxng_base_url({"configurable": {"user_id": "alice"}}) == "http://searxng:8080"


# --- the boto3 sites, which reach neither screen ---------------------------


def _aws_env(monkeypatch, module, *, keys=True):
    """Point a boto3 module's settings leg at operator-configured keys."""
    env = {
        "s3_access_key_id": "AKIAOPERATOR" if keys else None,
        "s3_secret_access_key": "operator-secret" if keys else None,
    }
    monkeypatch.setattr(module, "_settings_value", lambda name: env.get(name))

    captured = {}

    class FakeBoto:
        @staticmethod
        def client(service_name, **kwargs):
            captured.update(kwargs)
            return object()

    import sys

    monkeypatch.setitem(sys.modules, "boto3", FakeBoto)
    return captured


def test_a_planted_aws_endpoint_cannot_carry_the_operators_env_key_pair(vault_setup, monkeypatch):
    """The env leg, closed locally where it is concrete.

    Slice B guarantees a record supplying an address holds SOME anchor field,
    not the one the call site asks for. For aws the anchors are
    ``secret_access_key``/``session_token`` while ``access_key_id`` is a
    non-proof name, so ``endpoint_url`` + ``session_token`` clears slice B, both
    key lookups then miss the vault, and boto3 SigV4-signs the operator's
    env-configured key pair to the planted address.
    """
    from nymeria.tools import aws_service_integrations as tools
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    _mint(
        vault_setup,
        name="planted",
        provider="aws",
        secret_fields={"endpoint_url": "https://attacker.example.com", "session_token": "junk"},
    )
    captured = _aws_env(monkeypatch, tools)

    with pytest.raises(CredentialDestinationRefused):
        tools._aws_client("s3", "aws_ses_send_email", {"configurable": {"user_id": "alice"}})
    assert captured == {}, "boto3 was constructed before the refusal"


def test_a_deployment_with_no_keys_at_all_still_gets_the_setup_hint(vault_setup, monkeypatch):
    """The refusal must not pre-empt the friendly first-run message.

    A planted endpoint with no key pair anywhere has nothing to steer and
    nothing to steal, so the operator should be told to save an access key, not
    handed a refusal about an endpoint they did not configure. That is why the
    endpoint is resolved AFTER the setup-hint guard rather than before it.
    """
    from nymeria.tools import aws_service_integrations as tools

    _mint(
        vault_setup,
        name="planted",
        provider="aws",
        secret_fields={"endpoint_url": "https://attacker.example.com", "session_token": "junk"},
    )
    _aws_env(monkeypatch, tools, keys=False)

    client, error = tools._aws_client("s3", "aws_ses_send_email", {"configurable": {"user_id": "alice"}})
    assert client is None
    assert "access_key_id" in error


def test_an_aws_endpoint_from_the_record_that_owns_the_keys_is_still_supported(vault_setup, monkeypatch):
    """S3-compatible endpoints (MinIO, Wasabi, R2) keep working."""
    from nymeria.tools import aws_service_integrations as tools

    _mint(
        vault_setup,
        name="wasabi",
        provider="aws",
        secret_fields={
            "endpoint_url": "https://s3.wasabisys.example",
            "access_key_id": "AKIAOWNED",
            "secret_access_key": "owned-secret",
        },
    )
    captured = _aws_env(monkeypatch, tools)
    monkeypatch.setattr(http_policy, "_resolve_host", _public("93.184.216.34"))

    client, error = tools._aws_client("s3", "aws_ses_send_email", {"configurable": {"user_id": "alice"}})
    assert error is None and client is not None
    assert captured["endpoint_url"] == "https://s3.wasabisys.example"
    assert captured["aws_access_key_id"] == "AKIAOWNED"


def test_an_aws_endpoint_pointing_inward_is_refused_even_with_its_own_keys(vault_setup, monkeypatch):
    """boto3 never passes through ``base_url()`` or the httpx wrapper.

    So this screen is the only one the address gets, and it resolves DNS: the
    hostname form is the whole bypass everywhere else in this file.
    """
    from nymeria.tools import aws_service_integrations as tools

    _mint(
        vault_setup,
        name="inward",
        provider="aws",
        secret_fields={
            "endpoint_url": "http://localhost:9000",
            "access_key_id": "AKIAOWNED",
            "secret_access_key": "owned-secret",
        },
    )
    captured = _aws_env(monkeypatch, tools)

    with pytest.raises(ValueError, match="AWS endpoint URL blocked"):
        tools._aws_client("s3", "aws_ses_send_email", {"configurable": {"user_id": "alice"}})
    assert captured == {}


def test_aws_without_an_endpoint_override_is_untouched(vault_setup, monkeypatch):
    """The default path (no endpoint in the vault) still uses the env key pair."""
    from nymeria.tools import aws_service_integrations as tools

    captured = _aws_env(monkeypatch, tools)
    client, error = tools._aws_client("ses", "aws_ses_send_email", {"configurable": {"user_id": "alice"}})
    assert error is None and client is not None
    assert "endpoint_url" not in captured
    assert captured["aws_access_key_id"] == "AKIAOPERATOR"


def test_the_s3_module_pays_the_same_toll(vault_setup, monkeypatch):
    """``file_storage._s3_client`` is the second boto3 site, found by the gate.

    It was not in the slice-C scoping, which keyed on ``_base_url()`` absence and
    so could not see a module that calls it for its Dropbox and Nextcloud
    halves. The corpus gate surfaced it on its first run, which is the argument
    for having the gate at all.
    """
    from nymeria.tools import file_storage_service_integrations as tools
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    _mint(
        vault_setup,
        name="planted",
        provider="s3",
        secret_fields={"endpoint_url": "https://attacker.example.com", "session_token": "junk"},
    )
    _aws_env(monkeypatch, tools)

    with pytest.raises(CredentialDestinationRefused):
        tools._s3_client("s3_list_objects", {"configurable": {"user_id": "alice"}})


def test_an_operator_configured_endpoint_is_left_alone():
    """``S3_ENDPOINT_URL=http://minio:9000`` is an ordinary compose deployment."""
    assert (
        base.signed_endpoint_url(
            from_vault=None,
            from_settings="http://minio:9000",
            keys_from_vault=False,
            label="S3 endpoint URL",
        )
        == "http://minio:9000"
    )


def test_a_vault_endpoint_wins_over_the_settings_one_but_still_pays_the_toll():
    """Precedence is unchanged; the vault leg simply has to earn it."""
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    with pytest.raises(CredentialDestinationRefused):
        base.signed_endpoint_url(
            from_vault="https://attacker.example.com",
            from_settings="http://minio:9000",
            keys_from_vault=False,
            label="S3 endpoint URL",
        )


def test_no_test_in_this_file_needs_live_dns():
    """The suite has to pass on a network-isolated runner.

    Every hostname used above is either resolver-injected, monkeypatched, in a
    reserved TLD, or a literal IP. This pins the intent, since the natural way
    to write these tests reaches for example.com and passes here.
    """
    source = pathlib.Path(__file__).read_text()
    for name in ("example.com", "example.org", "google.com", "wasabisys.com"):
        assert f"//{name}" not in source, f"{name} would require live DNS"


# ---------------------------------------------------------------------------
# E10-02-D: the settings/env leg.
#
# Slice B guarantees a record supplying an address holds SOME anchor field of
# the provider's, not the one a given call site asks for. Where a provider
# declares two or more independent anchor groups, a record can therefore hold
# base_url plus an anchor this call site never asks for: it clears slice B, the
# secret lookup misses the vault, and the operator's env-configured secret goes
# out to that record's address. ``require_joined_destination`` is the join,
# spelled at the point the credential is committed to a request.
# ---------------------------------------------------------------------------


def test_the_guard_passes_every_shape_that_is_not_the_leak():
    """Three configurations must survive, or the control costs real deployments."""
    # No vault address: settings or the vendor default chose the destination, so
    # nothing caller-supplied is steering anything.
    base.require_joined_destination(
        destination_from_vault=None,
        secret_from_vault=None,
        secret="operator-key",
        provider="grafana",
    )
    # Address and secret from the same record: exactly what slice B arranges.
    base.require_joined_destination(
        destination_from_vault="https://self.hosted",
        secret_from_vault="record-key",
        secret="record-key",
        provider="grafana",
    )
    # Nothing resolved: the caller's own "no credential yet" branch returns a
    # setup hint, and refusing here would pre-empt that friendly first-run
    # message with a complaint about an endpoint the user never configured.
    base.require_joined_destination(
        destination_from_vault="https://self.hosted",
        secret_from_vault=None,
        secret=None,
        provider="grafana",
    )


def test_the_guard_refuses_the_leak_and_names_the_remedy():
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    with pytest.raises(CredentialDestinationRefused) as caught:
        base.require_joined_destination(
            destination_from_vault="https://attacker.invalid",
            secret_from_vault=None,
            secret="operator-key",
            provider="grafana",
        )
    message = str(caught.value)
    assert "grafana" in message
    # All THREE remedies, because the message shipped with only the first two and
    # was then unactionable for the case it fires on most: a record that already
    # holds a working credential of a different kind, with a stale environment
    # variable feeding an earlier auth branch. That reading told the operator to
    # do something they had already done. See task #65 for the version that
    # removes the refusal instead of explaining it.
    assert "Add that credential to the same record" in message
    assert "configure the address in settings" in message
    assert "stale environment variable" in message


def test_a_planted_elasticsearch_base_url_cannot_carry_the_operators_env_key(
    vault_setup, monkeypatch
):
    """The multi-secret case, where a disjunction guard would still leak.

    elasticsearch declares api_key, bearer_token and password as independent
    anchors. A record holding base_url + password clears slice B, so a guard
    asking "did the record supply ANY accepted credential" passes it, and the
    api_key branch then sends the operator's env key to that record's address.
    That is slice B's own weakness reproduced one level down, and it is why the
    guard runs per branch on the credential that actually authenticates.
    """
    from nymeria.tools import operations_monitoring_service_integrations as tools
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    _mint(
        vault_setup,
        name="planted",
        provider="elasticsearch",
        secret_fields={"base_url": "https://attacker.invalid", "password": "junk"},
    )
    monkeypatch.setattr(
        tools,
        "_settings_value",
        lambda name: "operator-key" if name == "elasticsearch_api_key" else None,
    )

    with pytest.raises(CredentialDestinationRefused):
        tools._elasticsearch_config(
            "elasticsearch_search", {"configurable": {"user_id": "alice"}}
        )


def test_a_self_hosted_record_holding_both_halves_still_works(vault_setup, monkeypatch):
    """The control must cost nothing to the ordinary self-hosted setup."""
    from nymeria.tools import operations_monitoring_service_integrations as tools

    _mint(
        vault_setup,
        name="mine",
        provider="elasticsearch",
        secret_fields={
            "base_url": "https://elastic.internal",
            "username": "elastic",
            "password": "s3cret",
        },
    )
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    resolved, headers, auth, _verify = tools._elasticsearch_config(
        "elasticsearch_search", {"configurable": {"user_id": "alice"}}
    )
    assert resolved == "https://elastic.internal"
    assert auth == ("elastic", "s3cret")
    assert isinstance(headers, dict)


# --- the ratchet -----------------------------------------------------------

_JOIN_GUARD = "require_joined_destination"

# The exemption marker lives at the CALL SITE, not in a table here, for the
# reason both sibling gates record (``test_exec_sandbox_gate.py``, quoting the
# env gate): a table keyed on a location breaks on any edit near the site, which
# trains people to re-point entries mechanically, and it puts the reason
# somewhere the person deleting the code will never look. A table keyed on a
# function NAME is the same failure one notch milder: it survives line edits and
# dies silently on a rename.
#
#     # join-gate: enforced-elsewhere - <reason>
#
# The siblings anchor their marker to one spawn with ``_markers_above``. This
# one is scoped to the whole function body instead, because the offence is not a
# single call: the join spans an address resolved in one statement and a secret
# resolved in another, and there is no one line to sit above. Comment lines
# only, so a string literal mentioning the marker cannot satisfy it.
_ENFORCED_ELSEWHERE = "join-gate: enforced-elsewhere"

# Set from a measured run, not chosen. See the floor assertion at the end of
# test_every_shaped_call_site_joins_its_destination_to_its_secret for why a
# rule alone is not a ratchet.
_SHAPED_SITE_FLOOR = 48
_GUARD_CALL_FLOOR = 79


def _call_name(node):
    func = getattr(node, "func", None)
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _resolved_field_names(value, module):
    """``field_names=`` as a concrete set, whether spelled literally or via group()."""
    if isinstance(value, (ast.Tuple, ast.List)):
        try:
            return set(ast.literal_eval(value))
        except Exception:
            return None
    if isinstance(value, ast.Call) and _call_name(value) == "group" and value.args:
        owner = getattr(value.func, "value", None)
        if isinstance(owner, ast.Name):
            spec = getattr(module, owner.id, None)
            try:
                return set(spec.group(ast.literal_eval(value.args[0])))
            except Exception:
                return None
    return None


def _resolved_spec(value, module):
    """The provider spec behind ``provider=``, however the call site spells it.

    Both spellings are load-bearing. ``provider=_GRAFANA.provider`` is the
    common one, but ``provider="aws"`` as a bare string is used too, and a gate
    resolving only the first silently SKIPS every function using the second.
    Returns None for ``provider=provider``, a parameter of a generic helper;
    that case is why the gate below must fail closed rather than skip.
    """
    from nymeria.tools.credential_registry import get_provider_spec

    if (
        isinstance(value, ast.Attribute)
        and value.attr == "provider"
        and isinstance(value.value, ast.Name)
    ):
        return getattr(module, value.value.id, None)
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return get_provider_spec(value.value)
    return None


def _lookup_wrappers(functions, module):
    """Same-module helpers that forward to the vault lookup, name -> their spec.

    ``aws`` and ``file_storage`` wrap ``_credential_value`` in a tiny local
    helper taking ``field_names``, and bind the provider inside it. A gate
    matching only the name ``_credential_value`` reads those modules as having
    no lookups at all.
    """
    wrappers = {}
    for fn in functions:
        parameters = {arg.arg for arg in fn.args.args} | {
            arg.arg for arg in fn.args.kwonlyargs
        }
        if "field_names" not in parameters:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and (_call_name(node) or "").endswith(
                "credential_value"
            ):
                keywords = {kw.arg: kw.value for kw in node.keywords or []}
                wrappers[fn.name] = _resolved_spec(keywords.get("provider"), module)
                break
    return wrappers


def _classify(spec, fields):
    """One of "address", "anchor", "anchor-complete", "metadata" or "unknown".

    "anchor" is the shaped one: this lookup asks for SOME of the provider's
    anchors but not all of them, so there is a gap. A record can hold an anchor
    outside the asked set, clear slice B's completeness rule with it, and miss
    this lookup, which then falls through to the operator's settings value.

    "anchor-complete" asks for the whole anchor set and is NOT shaped, and that
    conclusion DEPENDS ON a rule in another file. Slice B only lets the first
    record holding every anchor any candidate holds serve the address, so that
    record also answers any lookup covering the whole anchor set: same record,
    nothing to join. The dependency is that "holds" is judged by VALUE. While it
    was judged by NAME, a record could name the exact field with an empty value,
    clear slice B, and still miss the lookup, which made every one of these
    shaped after all (measured on hubspot: the operator's access token went to a
    planted base_url). ``_destination_record_id`` carries the other half of this
    note, and ``test_an_empty_anchor_field_is_not_possession`` pins it.

    PER SPEC, never against a union of names across providers. The union reads
    179 anchor groups as address lookups, because ``value``, ``domain``,
    ``host``, ``region`` and ``server`` are destination aliases for some provider
    and secret aliases for others. A gate that calls a secret lookup an address
    then drops it from its own analysis and reports the site clean, which is how
    the five generic helpers were skipped entirely.

    "unknown" is the important answer. It is returned whenever the provider or
    the field names are not statically resolvable, which is the shape of the
    generic ``_api_key_config`` family, and the caller treats it as fail-closed
    rather than skipping the function.
    """
    from nymeria.tools.credential_registry import (
        DESTINATION_URL_FIELDS,
        credential_anchor_fields,
        destination_url_fields_for,
    )

    if fields is None:
        return "unknown"
    if spec is None:
        # No spec, so only the PRIMARY names can be trusted: they are the ones
        # that are URL-shaped in every register entry that uses them. Anything
        # else here is unclassifiable rather than assumed harmless.
        return "address" if fields & DESTINATION_URL_FIELDS else "unknown"
    if fields & destination_url_fields_for(spec):
        return "address"
    anchors = credential_anchor_fields(spec)
    if fields & anchors:
        return "anchor" if anchors - fields else "anchor-complete"
    return "metadata"


def _credential_lookups(function, module, wrappers):
    """(spec_or_None, asked_field_names, callee) for every vault lookup here.

    The spec is None whenever the call site names its provider through a
    parameter, and the field names are None whenever they are not a literal
    tuple or a ``SPEC.group(...)`` call. Both are kept rather than dropped: a
    dropped entry is what makes a gate fail open, and ``_classify`` turns them
    into the "unknown" that forces a guard.
    """
    found = []
    for node in ast.walk(function):
        name = _call_name(node) if isinstance(node, ast.Call) else None
        if not name:
            continue
        direct = name.endswith("credential_value")
        if not direct and name not in wrappers:
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords or []}
        spec = _resolved_spec(keywords.get("provider"), module)
        if spec is None and not direct:
            spec = wrappers.get(name)
        fields = _resolved_field_names(keywords.get("field_names"), module)
        found.append((spec, fields, name))
    return found


def _guarded_provenance_names(function):
    """Locals passed as ``secret_from_vault=``, mapped to the guard's line.

    Line numbers matter because the name alone is not a key. Contentful's
    preview and delivery branches both assign ``token_from_vault``, so a
    name-keyed reading collapsed two credentials into one entry and let a single
    surviving guard cover both. Same shape in storyblok, spotify, reddit,
    pagerduty and mailjet.
    """
    names: dict[str, list[int]] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if _JOIN_GUARD not in (_call_name(node) or ""):
            continue
        for keyword in node.keywords or []:
            if keyword.arg == "secret_from_vault":
                for inner in ast.walk(keyword.value):
                    if isinstance(inner, ast.Name):
                        names.setdefault(inner.id, []).append(node.lineno)
    return names


def _secret_provenance_locals(function, module, wrappers):
    """Every ASSIGNMENT of an anchor lookup to a local: (local, provider, line).

    A list rather than a dict, and carrying the line, because the same local name
    is legitimately reused across mutually exclusive auth branches. Collapsing
    those to one entry is how a single guard came to cover two credentials.
    """
    found = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        call = node.value
        if not isinstance(target, ast.Name) or not isinstance(call, ast.Call):
            continue
        name = _call_name(call)
        if not name or not (name.endswith("credential_value") or name in wrappers):
            continue
        keywords = {kw.arg: kw.value for kw in call.keywords or []}
        spec = _resolved_spec(keywords.get("provider"), module)
        if spec is None and name in wrappers:
            spec = wrappers[name]
        fields = _resolved_field_names(keywords.get("field_names"), module)
        if _classify(spec, fields) == "anchor":
            found.append((target.id, spec.provider, node.lineno))
    return found


def _uncovered_provenance(function, module, wrappers):
    """Anchor assignments with no guard naming them before the local is rebound.

    The window is (this assignment, next assignment to the same name), so two
    branches assigning ``token_from_vault`` need two guards, and a guard placed
    ABOVE its assignment does not count: it would have read a stale or unbound
    local.

    A local this function RETURNS is exempt, because it has been handed to a
    caller to join and this function is not where that decision lives.
    ``_reddit_access_token`` is the case: it resolves the bearer but never sees
    the base URL that bearer rides to, so its join can only be written in
    ``_reddit_config``, one frame up. Reporting it here would demand a guard at
    a site with nothing to guard against.

    The residual is stated rather than closed: this exempts the callee without
    proving the caller joins THAT local specifically. The caller is still
    required to carry a guard, so the exemption cannot silently drop a whole
    function, but pairing a returned provenance to the address it is joined
    against is the third of the three known ratchet holes (see the task filed
    against this file).
    """
    returned = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        for inner in ast.walk(node.value):
            if isinstance(inner, ast.Name):
                returned.add(inner.id)
    assignments = [
        entry
        for entry in _secret_provenance_locals(function, module, wrappers)
        if entry[0] not in returned
    ]
    guards = _guarded_provenance_names(function)
    rebound = {}
    for local, _provider, line in assignments:
        rebound.setdefault(local, []).append(line)
    uncovered = []
    for local, provider, line in assignments:
        later = [other for other in rebound[local] if other > line]
        end = min(later) if later else function.end_lineno + 1
        if not any(line < guard < end for guard in guards.get(local, ())):
            uncovered.append((local, provider))
    return uncovered


def test_every_shaped_call_site_joins_its_destination_to_its_secret():
    """A call site that can be steered must refuse to carry the server's secret.

    Two rules, because one alone leaves a hole each way.

    PRECISE, where the provider resolves statically: the function resolves a
    whole ADDRESS for a provider from the vault and also resolves one of that
    provider's anchors with a field set NARROWER than the whole anchor set. The
    gap is exactly the set of fields a record can hold to clear slice B while
    missing this lookup. Every such anchor must be named by some guard, since
    presence of SOME guard is not coverage: a provider with three alternative
    credentials has three branches, and deleting one guard leaves two behind.

    FAIL CLOSED, where it does not: the generic ``_api_key_config`` family takes
    ``provider`` as a parameter and ``field_names`` from its caller, so neither
    resolves and the precise rule sees nothing. Those functions must carry a
    guard regardless. Without this the gate was blind to 14 of its own guard
    calls.

    Classification is PER SPEC (``_classify``), never against a union of names
    across providers, and the names come from ``credential_registry`` rather than
    a list here. Both halves were learned the expensive way. A hand-copied list
    held 18 names against the register's 26 URL primaries, and the 17 it missed
    included ``token_url``, the address Reddit's client secret is POSTed to. The
    global union that replaced it reads 179 anchor groups across 176 providers as
    address lookups, because ``value``, ``domain``, ``host``, ``region`` and
    ``server`` are destination aliases somewhere and secret aliases elsewhere;
    a lookup misread that way is dropped from the analysis and its site reported
    clean.

    Both an ADDRESS helper and a SECRET helper propagate into their caller. Only
    the first is obvious. Without the second, a pairing split across two sibling
    helpers is invisible in all three functions involved, which is how ghost's
    admin JWT and graphql's api-key header stayed unguarded. A helper that holds
    a guard of its own hands its address up but not its anchor.

    WHAT THIS GATE CANNOT SEE. Read a green run as "no NEW unguarded site of a
    shape this gate recognises was introduced", not as "no site can leak".

    * Host FRAGMENT destinations (``DESTINATION_HOST_FRAGMENT_FIELDS``), out of
      scope deliberately and tracked as E10-02-E. Where the site does not
      validate the fragment, ``/`` or ``#`` terminates the authority and it
      escapes the vendor; where it does, a planted value still reaches another
      TENANT of the same vendor. Both want fixing, neither is this rule.
    * Whether a guard is placed CORRECTLY, beyond ordering. It requires a guard
      naming each anchor local between that lookup and the next rebinding of the
      name, which catches a guard above its own assignment and a second branch
      reusing a name. It does NOT check that the guard sits before the header is
      built, nor that the code path is reachable: a guard in dead code counts.
    * Whether a self-joining helper joins against the SAME address its caller
      uses. A helper holding a guard is trusted to have joined its own pair.
    * A guard passed a provenance local that is not the one the request actually
      carries. ``secret_from_vault=x`` is taken at its word.
    * The ``join-gate: enforced-elsewhere`` marker is matched against every
      comment line in the function. An unrelated comment quoting it disarms both
      the exemption and the self-joining inference for that function. The string
      is distinctive enough that this is a hazard rather than a hole, but it is
      one, and a marker is meant to be read by a person reviewing the site.
    * Anything reached through ``getattr``, a dispatch table, or a lookup whose
      provider or field names are computed at runtime. Where the names are not
      statically resolvable ``_classify`` returns "unknown" and the function is
      required to carry a guard, so this fails closed rather than silent, but a
      required guard is not the same as a correct one.

    Hence the floor below, which is the crude half of the ratchet: the rule
    catches a guard deleted from a site it still recognises, and the floor
    catches a refactor that stops it recognising the sites at all. Raise the
    floor when a change legitimately adds sites; a DROP wants explaining.
    """
    problems = []
    shaped = 0
    guard_calls = 0
    # ALL of nymeria/tools/, like the three gates above, not the
    # *_service_integrations.py glob. The glob was this file's own first mistake
    # (see the module docstring), and it repeated here: `graphql` in
    # developer_platform_integrations.py resolves an `endpoint` from the vault
    # and an `api_key` that falls back to settings, has zero guards, and was
    # invisible. `_tools_modules()` is the shared scope.
    for path in _tools_modules():
        if path.name == "__init__.py":
            continue
        module = importlib.import_module(f"nymeria.tools.{path.stem}")
        source = path.read_text()
        tree = ast.parse(source)
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        wrappers = _lookup_wrappers(functions, module)
        # Functions that hold a guard of their own. Two DIFFERENT delegations
        # hang off this, and they need different evidence:
        #
        #  1. A lookup wrapper that resolves BOTH halves itself
        #     (``_api_key_config``). The caller hands it ``field_names`` and
        #     nothing else, so demanding a second guard at the call site would be
        #     asking for one that can see neither half. Delegated only when the
        #     caller resolves no address of its own, since otherwise the wrapper
        #     is joining against a DIFFERENT address than the caller's.
        #  2. A secret helper the caller hands its address provenance down to
        #     (``_ghost_admin_headers(site_from_vault=...)``). Here the caller
        #     does have its own address, and passing a ``*_from_vault`` argument
        #     is the evidence that the two halves met.
        #
        # Without the second, the pairing is invisible whenever the secret lives
        # in a sibling helper rather than in the caller: ghost's admin JWT,
        # graphql's api-key header and gotify's per-kind token were all missed
        # that way, which makes it a class rather than an oversight.
        source_lines = source.splitlines()

        def _has_marker(fn):
            return any(
                _ENFORCED_ELSEWHERE in line
                for line in source_lines[fn.lineno - 1 : fn.end_lineno]
                if line.lstrip().startswith("#")
            )

        # A marked function counts as self-joining too. ``_s3_client`` is the
        # case: signed_endpoint_url is the join there, expressed as a drop rather
        # than a raise, so it answers for its own pair exactly as a guard call
        # would and its nine tool callers should not each be asked again.
        self_joining = {
            fn.name
            for fn in functions
            if _has_marker(fn)
            or any(
                _call_name(node) and _JOIN_GUARD in _call_name(node)
                for node in ast.walk(fn)
                if isinstance(node, ast.Call)
            )
        }
        guarded_wrappers = {name for name in self_joining if name in wrappers}

        # Helpers that resolve one of a provider's ANCHORS. A call to one is an
        # anchor lookup in the caller, exactly like an inline one.
        secret_helpers = {}
        for fn in functions:
            specs = {
                spec
                for spec, f, _c in _credential_lookups(fn, module, wrappers)
                if spec is not None and _classify(spec, f) == "anchor"
            }
            if specs and fn.name not in wrappers:
                secret_helpers[fn.name] = specs

        # Helpers that hand a vault-supplied address back, and any spec they
        # bind themselves. Deliberately NOT keyed on the spec resolving here:
        # the two ``_service_base`` helpers take ``provider`` as a parameter, so
        # keying on it emptied this set and made every self-hosted caller
        # invisible.
        address_helpers = {}
        for fn in functions:
            lookups = _credential_lookups(fn, module, wrappers)
            if any(_classify(spec, f) == "address" for spec, f, _c in lookups):
                address_helpers[fn.name] = {
                    spec
                    for spec, f, _c in lookups
                    if spec is not None and _classify(spec, f) == "address"
                }

        for fn in functions:
            lookups = _credential_lookups(fn, module, wrappers)
            kinds = [
                (_classify(spec, fields), spec, callee)
                for spec, fields, callee in lookups
            ]
            own_url_lookup = any(
                kind == "address"
                for kind, _spec, callee in kinds
                if callee not in guarded_wrappers
            )
            resolves_address = any(kind == "address" for kind, _s, _c in kinds)
            destinations = {
                spec
                for kind, spec, _callee in kinds
                if kind == "address" and spec is not None
            }
            # A caller inherits both halves from the helpers it calls: the
            # destination of an address helper, and the anchor of a secret
            # helper. The provider may be named at the call site
            # (``_service_base``) or bound inside the helper
            # (``_spotify_accounts_base``); take both.
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                callee = _call_name(node)
                if not callee or callee == fn.name:
                    continue
                keywords = {kw.arg: kw.value for kw in node.keywords or []}
                # A self-joining helper hands its ADDRESS up but not its anchor:
                # it already joined its own pair, and suppressing the address too
                # would hide an anchor the CALLER resolves against that same
                # address (facebook_page_create_post's app_secret is that case).
                if callee in secret_helpers and callee not in self_joining:
                    kinds.append(("anchor", next(iter(secret_helpers[callee])), callee))
                if callee not in address_helpers:
                    continue
                resolves_address = True
                if callee not in guarded_wrappers:
                    own_url_lookup = True
                spec = _resolved_spec(keywords.get("provider"), module)
                if spec is not None:
                    destinations.add(spec)
                destinations |= address_helpers[callee]
            if not resolves_address:
                continue

            # An anchor is a field that PROVES the caller holds the account:
            # everything the register classes as neither a destination nor
            # public metadata. ``username``, ``api_version`` and ``instance``
            # are not anchors, so a function resolving only those alongside an
            # address has nothing worth stealing and is not shaped.
            delegated = lambda callee: (  # noqa: E731
                callee in guarded_wrappers and not own_url_lookup
            )
            anchor_lookups = [
                spec
                for kind, spec, callee in kinds
                if kind == "anchor"
                and (spec in destinations or not destinations)
                and not delegated(callee)
            ]
            unresolvable = [
                callee
                for kind, _spec, callee in kinds
                if kind == "unknown" and not delegated(callee)
            ]
            if not anchor_lookups and not unresolvable:
                continue
            shaped += 1
            guard_calls += sum(
                1
                for node in ast.walk(fn)
                if isinstance(node, ast.Call)
                and _call_name(node)
                and _JOIN_GUARD in _call_name(node)
            )

            if _has_marker(fn):
                continue
            guarded = any(
                _call_name(node) and _JOIN_GUARD in _call_name(node)
                for node in ast.walk(fn)
                if isinstance(node, ast.Call)
            )
            if not guarded:
                why = (
                    "an anchor credential"
                    if anchor_lookups
                    else "a credential whose provider this gate cannot resolve"
                )
                problems.append(
                    f"{path.name}::{fn.name} resolves an address from the vault and "
                    f"then resolves {why}, with no join guard and no "
                    f"'{_ENFORCED_ELSEWHERE}' marker"
                )
                continue

            for local, provider in _uncovered_provenance(fn, module, wrappers):
                problems.append(
                    f"{path.name}::{fn.name} resolves {provider}'s {local} but no "
                    "guard names it between that lookup and the next rebinding, "
                    "so that credential can still ride to a vault-supplied address"
                )
    assert not problems, (
        "these call sites can send a settings-configured secret to a "
        "vault-supplied address:\n  " + "\n  ".join(sorted(set(problems)))
    )
    # The floor. The rule above only fires on a site it still RECOGNISES, so a
    # refactor that changes how a destination or a provider is spelled would
    # empty the analysis and pass. These two numbers are what makes that visible.
    assert shaped >= _SHAPED_SITE_FLOOR and guard_calls >= _GUARD_CALL_FLOOR, (
        f"the join analysis now sees {shaped} shaped sites carrying "
        f"{guard_calls} guards, against a floor of {_SHAPED_SITE_FLOOR} and "
        f"{_GUARD_CALL_FLOOR}. Sites did not stop being shaped by themselves: "
        "either a lookup is spelled in a way _resolved_field_names or "
        "_resolved_spec no longer follows, or coverage really was removed.\n"
        "READ THIS BEFORE LOWERING IT. A drop is not automatically a defect. "
        "Moving a guard INTO a secret helper makes that helper self-joining, "
        "and the gate then stops attributing its anchor to the caller, so the "
        "caller correctly stops being shaped and the count falls by one. Two "
        "independent fixes hit this during the pass that introduced the rule. "
        "So: identify the exact function that changed, confirm it is either "
        "self-joining now or genuinely no longer shaped, and only then move the "
        "floor. What you must never do is lower it without naming the function."
    )
