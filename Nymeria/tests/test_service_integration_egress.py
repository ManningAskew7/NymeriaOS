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
import re
import socket

import httpx
import pytest
from cryptography.fernet import Fernet

from test_subprocess_env_gate import _markers_above

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

# The crude half of the ratchet: WHICH sites this analysis recognises, and how
# many join guards each one carries.
#
# The rule catches a guard deleted from a site it still recognises. It cannot
# catch a refactor that stops it recognising the site at all, so something has
# to pin the recognised set. That used to be two integers
# (``_SHAPED_SITE_FLOOR = 48``, ``_GUARD_CALL_FLOOR = 79``), and audit task #69
# replaced them for the reason the sibling gates already record about tables
# keyed on locations: a count cannot tell one site from another. When the set
# moved by four during that task, the failure said only that four sites had
# gone, and naming them took a bisect against the committed version of this file
# with a debug print patched into both copies.
#
# The COUNT half is not decoration, and review supplied the witness. A site can
# stay recognised while one of its guards disappears: contentful resolves a
# preview token and a delivery token against two addresses, so refactoring its
# two branches into a ladder and deleting one branch's guard left the site in
# the register, the rule satisfied by the surviving guard, and the old
# ``_GUARD_CALL_FLOOR`` was the only thing that failed. Dropping that integer
# for a name-only set was a net LOSS of coverage until this became a mapping.
#
# Measured, never hand-written: the entries come from a run, and the assertion
# compares mappings, so an addition, a removal and a changed count are each
# loud. Adding a site is expected whenever a new integration resolves both
# halves; removing one, or lowering a count, is what wants a reason.
_SHAPED_SITES = {
    "aws_service_integrations.py::_aws_client": 0,
    "bookmark_link_service_integrations.py::_yourls_endpoint": 2,
    "business_service_integrations.py::_bearer_config": 1,
    "chat_platform_service_integrations.py::_api_token_config": 1,
    "commerce_billing_service_integrations.py::_shopify_config": 3,
    "commerce_billing_service_integrations.py::_woocommerce_config": 2,
    "community_publishing_service_integrations.py::_bearer_service_config": 1,
    "community_publishing_service_integrations.py::_facebook_config": 1,
    "community_publishing_service_integrations.py::_reddit_access_token": 2,
    "community_publishing_service_integrations.py::_reddit_base": 0,
    "community_publishing_service_integrations.py::facebook_page_create_post": 0,
    "content_management_service_integrations.py::_contentful_config": 2,
    "content_management_service_integrations.py::_storyblok_content_config": 1,
    "content_management_service_integrations.py::_storyblok_management_config": 1,
    "content_management_service_integrations.py::_strapi_config": 2,
    "customer_engagement_service_integrations.py::_mailchimp_config": 2,
    "customer_engagement_service_integrations.py::_zendesk_config": 2,
    "data_table_service_integrations.py::_api_key_config": 1,
    "enrichment_security_service_integrations.py::_api_key_config": 1,
    "enrichment_security_service_integrations.py::_elastic_security_config": 2,
    "enrichment_security_service_integrations.py::_jina_base": 0,
    "enterprise_business_service_integrations.py::_erpnext_config": 2,
    "enterprise_business_service_integrations.py::_invoiceninja_config": 2,
    "event_meeting_service_integrations.py::_demio_config": 2,
    "file_storage_service_integrations.py::_nextcloud_config": 2,
    "file_storage_service_integrations.py::_s3_client": 0,
    "lead_enrichment_service_integrations.py::_clearbit_base": 0,
    "lead_enrichment_service_integrations.py::_clearbit_headers": 0,
    "lead_enrichment_service_integrations.py::_dropcontact_config": 1,
    "lead_enrichment_service_integrations.py::_humantic_config": 1,
    "lead_enrichment_service_integrations.py::_lonescale_config": 1,
    "lead_enrichment_service_integrations.py::_uplead_config": 1,
    "marketing_contact_service_integrations.py::_customerio_config": 2,
    "marketing_contact_service_integrations.py::_mautic_config": 2,
    "marketing_contact_service_integrations.py::_token_config": 1,
    "media_discovery_service_integrations.py::_spotify_config": 3,
    "messaging_delivery_service_integrations.py::_mailjet_email_config": 2,
    "messaging_delivery_service_integrations.py::_mailjet_sms_config": 1,
    "messaging_delivery_service_integrations.py::_mocean_config": 2,
    "messaging_delivery_service_integrations.py::_vonage_config": 2,
    "notification_service_integrations.py::_gotify_config": 2,
    "operations_monitoring_service_integrations.py::_bearer_config": 1,
    "operations_monitoring_service_integrations.py::_elasticsearch_config": 3,
    "operations_monitoring_service_integrations.py::_metabase_config": 3,
    "operations_monitoring_service_integrations.py::_pagerduty_config": 2,
    "personal_device_service_integrations.py::_bearer_config": 1,
    "productivity_service_integrations.py::_trello_config": 2,
    "project_management_service_integrations.py::_jira_config": 2,
    "project_management_service_integrations.py::_taiga_config": 2,
    "project_management_service_integrations.py::_wekan_config": 2,
    "sales_crm_service_integrations.py::_pipedrive_config": 2,
    "support_service_integrations.py::_servicenow_config": 2,
    "support_service_integrations.py::_zammad_config": 2,
}


def _call_name(node):
    func = getattr(node, "func", None)
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _resolved_field_names(value, module):
    """``field_names=`` as a concrete set, whether spelled literally or via group().

    Deliberately NARROW, and audit task #69 is the record of why. It was widened to
    follow an ``ast.IfExp``, a local bound by an if/elif/else ladder, a
    partially literal tuple and a callee's default argument, so that four
    helpers using those spellings would be classified precisely instead of
    falling to the fail-closed "unknown" rule. Review measured that the widening
    LOST coverage on net, and it is worth stating both halves because the second
    is the one that is easy to talk yourself out of.

    It over-resolves. Unioning the arms of a branch produces a field set wider
    than any single execution path, and ``_classify`` flips "anchor" to
    "anchor-complete" the moment ``anchors - fields`` empties, which drops the
    lookup from the analysis entirely. Measured on contentful, whose
    ``preview_token`` and ``delivery_token`` groups union to its complete anchor
    set by construction: refactor the two branches into a ladder, delete one
    branch's guard, and the widened gate goes green where this one fails.

    And under-resolution is NOT safe in both directions, which the widened
    version's own docstring claimed. There is a third outcome besides "wider
    gap" and "unknown": a partial tuple resolving to ``{"space_id"}``
    classifies "metadata" and is dropped, where ``None`` classified "unknown"
    and demanded a guard. A short field set can shrink the analysis.

    The four helpers are marked at their call sites instead, which is what the
    exemption marker is for. That is not a retreat to prose: they are clean
    because they carry NO anchor lookup at all, and the arithmetic that could
    change that is watched on their tool CALLERS, which stay in the analysis
    either way.
    """
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


def _parameterised_lookups(function, own_params):
    """The CALL NODES in ``function`` that delegate their subject to the caller.

    A lookup like ``credential_value(provider=provider, field_names=field_names)``
    is unresolvable HERE by construction: the subject comes from the caller. That
    is delegation, not an unknown, and it is detected by argument SHAPE rather
    than by a list of helper names, so a new generic helper is covered the day it
    is written.

    Nodes, not callee names, and that distinction is the whole correctness of the
    exemption. Keyed by name, ONE genuine delegation exempts every other call to
    the same callee in the same function, including a real unknown::

        def h(provider, field_names):
            a = _credential_value(provider=provider, field_names=field_names)
            b = _credential_value(provider="contentful", field_names=computed())

    Under a name-keyed reading ``b`` is exempted by its association with ``a``,
    which is precisely the fail-open the rule below exists to prevent. Detecting
    delegation by shape and then recording it by name would have undone the
    detection.
    """
    delegated = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords or []:
            # EITHER half being a parameter is enough. aws keeps a module-local
            # `_credential_value` that binds the provider itself and takes only
            # `field_names` from its caller, so keying on `provider` alone left
            # it demanding a guard for a decision it does not make.
            if (
                keyword.arg in {"provider", "field_names"}
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id in own_params
            ):
                delegated.add(id(node))
    return delegated


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
    """(spec_or_None, asked_field_names, callee, call_node) per vault lookup.

    The spec is None whenever the call site names its provider through a
    parameter, and the field names are None whenever they are not a literal
    tuple or a ``SPEC.group(...)`` call. Both are kept rather than dropped: a
    dropped entry is what makes a gate fail open, and ``_classify`` turns them
    into the "unknown" that forces a guard.

    The NODE rides along so the delegation exemption can key on this exact call
    rather than on its callee's name, which would let one genuine delegation
    exempt an unrelated unknown to the same helper. See
    ``_parameterised_lookups``.
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
        found.append((spec, fields, name, node))
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


def _flatten_or(value):
    """Every operand of an ``or`` chain, flattened through nesting."""
    if not isinstance(value, ast.BoolOp) or not isinstance(value.op, ast.Or):
        return [value]
    out = []
    for operand in value.values:
        out.extend(_flatten_or(operand))
    return out


def _or_chained_anchors(function, module, wrappers, provenance_names):
    """Anchor lookups buried in an ``or`` chain: (local, provider, line, keeps).

    RATCHET HOLE 1a. ``_secret_provenance_locals`` only reads an assignment whose
    whole value is a call, so ``x = _credential_value(...) or _settings_value(...)``
    was outside the per-local rule entirely. That spelling is not exotic, it is
    the house style for "vault, else the deployment's setting", and it is exactly
    the shape that made the reddit leak invisible.

    ``keeps`` is the half that makes this a two-part answer rather than one more
    thing to flag. An ``or`` chain preserves vault provenance only if EVERY
    operand is vault-sourced. Pagerduty writes
    ``api_token_from_vault = access_token_from_vault or _credential_value(...)``,
    where both arms come from the vault, so the local genuinely is a provenance
    bit and may be passed to a guard. The moment one operand is
    ``_settings_value(...)``, the local may hold the operator's own value, and
    passing it as ``secret_from_vault=`` would ASSERT vault provenance the code
    does not have, which is worse than not guarding: it is a guard that always
    says yes.
    """
    found = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        operands = _flatten_or(node.value)
        if len(operands) < 2:
            continue  # a bare call, already handled
        anchors = []
        vault_sourced = True
        for operand in operands:
            if isinstance(operand, ast.Name) and operand.id in provenance_names:
                continue
            if not isinstance(operand, ast.Call):
                vault_sourced = False
                continue
            name = _call_name(operand)
            if not name or not (name.endswith("credential_value") or name in wrappers):
                vault_sourced = False
                continue
            keywords = {kw.arg: kw.value for kw in operand.keywords or []}
            spec = _resolved_spec(keywords.get("provider"), module)
            if spec is None and name in wrappers:
                spec = wrappers[name]
            fields = _resolved_field_names(keywords.get("field_names"), module)
            if _classify(spec, fields) == "anchor":
                anchors.append(spec.provider)
        for provider in anchors:
            found.append((target.id, provider, node.lineno, vault_sourced))
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
    bare = _secret_provenance_locals(function, module, wrappers)
    # HOLE 1a. An anchor resolved inside an ``or`` chain was invisible here.
    # Two outcomes, because the chain either preserves vault provenance or
    # destroys it, and only one of them is answerable by a guard.
    chained = _or_chained_anchors(
        function, module, wrappers, {name for name, _p, _l in bare}
    )
    assignments = [entry for entry in bare if entry[0] not in returned]
    assignments += [
        (local, provider, line)
        for local, provider, line, keeps in chained
        if keeps and local not in returned
    ]
    # NO ``returned`` exemption on this class, and the difference is the whole
    # point of the exemption. It exists because a helper can hand its provenance
    # UP for the caller to join (``_reddit_access_token``). A local whose
    # provenance was DISCARDED hands up a value with no provenance attached, so
    # returning it does not move the decision anywhere, it deletes it. Caught by
    # mutation: collapsing taiga's ``token_from_vault`` into an ``or`` chain went
    # unreported because ``token`` also appears inside a ``_setup_hint(...)``
    # call in a return, which the crude name-anywhere-in-a-return scan counts.
    discarded = [
        (local, provider)
        for local, provider, _line, keeps in chained
        if not keeps
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
            uncovered.append((local, provider, "unguarded"))
    uncovered += [(local, provider, "discarded") for local, provider in discarded]
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
      scope deliberately. The half where the fragment left the vendor entirely
      is closed, by ``vendor_host`` and the two gates at the end of this file
      (E10-02-E). The half that remains is this rule's own kind of question and
      still is not answered here: a CONSTRAINED fragment reaches another TENANT
      of the same vendor, and the reason this gate cannot ask is mechanical
      rather than deliberate. Those lookups resolve to ``provider=None,
      fields=None`` and are dropped by ``if not resolves_address: continue``
      before the fail-closed "unknown" rule below can require a guard. Widening
      the join to fragments is therefore blocked on fixing that ordering (task
      #69), and adding a fragment rule first would add it to an analysis that
      cannot see the functions it applies to.
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

    Hence ``_SHAPED_SITES`` below, which is the crude half of the ratchet: the
    rule catches a guard deleted from a site it still recognises, and the
    mapping catches a refactor that stops it recognising the site, or that
    leaves the site recognised while one of its guards goes. Both halves are
    needed and review supplied the witness for the second, so read the comment
    on the mapping before changing it.
    """
    problems = []
    shaped = {}
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
                for spec, f, _c, _n in _credential_lookups(fn, module, wrappers)
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
            if any(_classify(spec, f) == "address" for spec, f, _c, _n in lookups):
                address_helpers[fn.name] = {
                    spec
                    for spec, f, _c, _n in lookups
                    if spec is not None and _classify(spec, f) == "address"
                }

        for fn in functions:
            lookups = _credential_lookups(fn, module, wrappers)
            kinds = [
                (_classify(spec, fields), spec, callee, node)
                for spec, fields, callee, node in lookups
            ]
            own_url_lookup = any(
                kind == "address"
                for kind, _spec, callee, _node in kinds
                if callee not in guarded_wrappers
            )
            resolves_address = any(kind == "address" for kind, _s, _c, _n in kinds)
            destinations = {
                spec
                for kind, spec, _callee, _node in kinds
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
                    kinds.append(
                        ("anchor", next(iter(secret_helpers[callee])), callee, node)
                    )
                if callee not in address_helpers:
                    continue
                resolves_address = True
                if callee not in guarded_wrappers:
                    own_url_lookup = True
                spec = _resolved_spec(keywords.get("provider"), module)
                if spec is not None:
                    destinations.add(spec)
                destinations |= address_helpers[callee]
            # RATCHET HOLE 2, closed. This skip used to run BEFORE the
            # fail-closed "unknown" rule below, so a function whose ADDRESS
            # lookup was itself unresolvable dropped out of the analysis
            # entirely rather than being required to carry a guard. Fail-closed
            # has to be checked FIRST or it is not fail-closed: "I could not
            # tell what this resolves" must mean "so it needs a guard", never
            # "so skip it".
            #
            # The exemption beside it is where the real hole was, and it took
            # two tries to state correctly. The STRUCTURAL rule is the durable
            # one: a lookup whose provider or field names are this function's
            # own PARAMETER is not decided here, so a function making exactly
            # ONE such lookup cannot be a pair and has nothing to join. Its
            # caller does. `credential_value`, `resolve_native_credential` and
            # lead enrichment's `_api_key` are that shape.
            #
            # A function making TWO OR MORE holds both values at once, so it CAN
            # join, whoever chose the subject. The first version missed that and
            # exempted every parameterised helper, which also exempted the
            # shared `_bearer_config` helpers, the only thing holding the philips
            # hue leak closed (that leak is `personal_device_service_
            # integrations.py`, whose `_philips_hue_config` calls the
            # `_bearer_config` in the same module; there are three functions by
            # that name in `tools/` and an earlier draft of this comment named
            # the wrong one).
            #
            # Corroboration, NOT the rule, because a corpus statistic rots: of
            # the 24 functions containing a parameterised lookup today, the 8
            # making two or more lookups all carry a guard and the 16 making one
            # carry none. Where it fails it fails OPEN, so a single-lookup helper
            # that grows a second half is exempted until the count moves.
            #
            # What this bought: `personal_device_service_integrations.py::
            # _bearer_config` was not merely unwatched, its whole module was. It
            # is not recognised as a lookup wrapper either (the wrapper detector
            # keys on the helper binding a concrete provider, which this one does
            # not), so nothing propagated to its three callers and the guard on a
            # leak this pass MEASURED could be deleted with the suite green.
            # Verified by deleting it, before and after.
            own_params = {
                arg.arg
                for arg in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs
            }
            # `id()` keys are safe here only because `kinds` holds every node
            # for the life of this loop; do not hoist this set out of it.
            delegating = (
                _parameterised_lookups(fn, own_params) if len(kinds) < 2 else set()
            )
            unresolved_here = [
                callee
                for kind, _spec, callee, node in kinds
                if kind == "unknown" and id(node) not in delegating
            ]
            if not resolves_address and not unresolved_here:
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
                for kind, spec, callee, _node in kinds
                if kind == "anchor"
                and (spec in destinations or not destinations)
                and not delegated(callee)
            ]
            unresolvable = [
                callee
                for kind, _spec, callee, _node in kinds
                if kind == "unknown" and not delegated(callee)
            ]
            if not anchor_lookups and not unresolvable:
                continue
            # The guard COUNT, not just the site name. A site can stay
            # recognised while one of its guards disappears: contentful resolves
            # a preview token and a delivery token against two addresses, and
            # deleting one branch's guard leaves the other holding the site in
            # the register. Counting per site is what makes that visible.
            shaped[f"{path.name}::{fn.name}"] = sum(
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

            for local, provider, why in _uncovered_provenance(fn, module, wrappers):
                if why == "discarded":
                    problems.append(
                        f"{path.name}::{fn.name} resolves {provider}'s anchor inside "
                        f"an 'or' chain assigned to {local}, alongside a non-vault "
                        "operand, so NO local carries its vault provenance and no "
                        f"guard can name it. Split the lookup out ({local}_from_vault "
                        "= ...) and guard on that"
                    )
                    continue
                problems.append(
                    f"{path.name}::{fn.name} resolves {provider}'s {local} but no "
                    "guard names it between that lookup and the next rebinding, "
                    "so that credential can still ride to a vault-supplied address"
                )
    assert not problems, (
        "these call sites can send a settings-configured secret to a "
        "vault-supplied address:\n  " + "\n  ".join(sorted(set(problems)))
    )
    # The rule above only fires on a site it still RECOGNISES, and on a guard
    # it can attribute to a specific anchor local. A refactor that stops it
    # recognising the site, or that leaves the site recognised while one of its
    # guards disappears, would otherwise quietly empty the analysis and pass.
    # The mapping is what makes both visible, and it names the function rather
    # than moving a number.
    dropped = sorted(set(_SHAPED_SITES) - set(shaped))
    added = sorted(set(shaped) - set(_SHAPED_SITES))
    thinned = sorted(
        f"{site}: {_SHAPED_SITES[site]} -> {shaped[site]}"
        for site in set(_SHAPED_SITES) & set(shaped)
        if shaped[site] < _SHAPED_SITES[site]
    )
    grew = sorted(
        f"{site}: {_SHAPED_SITES[site]} -> {shaped[site]}"
        for site in set(_SHAPED_SITES) & set(shaped)
        if shaped[site] > _SHAPED_SITES[site]
    )
    assert not dropped and not added and not thinned and not grew, (
        "the join analysis no longer sees what it saw.\n"
        f"  NO LONGER RECOGNISED ({len(dropped)}): {dropped}\n"
        f"  FEWER GUARDS ({len(thinned)}): {thinned}\n"
        f"  NEWLY RECOGNISED ({len(added)}): {added}\n"
        f"  MORE GUARDS ({len(grew)}): {grew}\n"
        "The first two are the ones that matter. A site does not stop being "
        "recognised, and does not shed a guard, by itself: either a lookup is "
        "now spelled in a way _resolved_field_names or _resolved_spec no longer "
        "follows, or coverage really was removed.\n"
        "READ THIS BEFORE EDITING _SHAPED_SITES. A drop is not automatically a "
        "defect, and there is one benign cause on record: moving a guard INTO a "
        "secret helper makes that helper self-joining, so the gate stops "
        "attributing its anchor to the caller and the caller correctly leaves "
        "the mapping. Two independent fixes hit that during the pass that "
        "introduced the rule. It is not a licence to edit the mapping to match. "
        "Name the function, say which cause applies, and put the reason in the "
        "commit message. The last two lines need no ceremony: more coverage is "
        "the direction this is supposed to move."
    )

# --- E10-02-E: a vault-supplied host FRAGMENT must not leave the vendor domain


def test_a_planted_fragment_cannot_move_the_netloc_off_the_vendor():
    """The measured escape, closed. Do not simplify this to a charset assertion.

    What matters is not that the value is rejected but that the RESULT cannot
    become someone else's netloc, so this asserts on ``urlparse`` output rather
    than on the helper's return value. The four terminators each move the vendor
    suffix into a different URL component, which is why a blacklist of one of
    them (the original finding said "#") would have looked like a fix.
    """
    from urllib.parse import urlparse

    from nymeria.tools.native_credentials import CredentialDestinationRefused
    from nymeria.tools.service_integration_base import vendor_host

    escapes = [
        "evil.com/x#",          # suffix pushed into the path
        "evil.com?",            # into the query
        "evil.com#",            # into the fragment
        "evil.com:443#",        # into a port, then the fragment
        "https://evil.com/x",   # scheme stripped, slash remains
        "a@evil.com",           # userinfo, measured NOT an escape but still refused
        "evil.com\\",           # backslash, measured NOT an escape but still refused
        "evil .com",            # would be percent-encoded downstream
        "evil.com\ty",          # would have the tab deleted downstream
        "acme\u3002evil.com",    # IDEOGRAPHIC FULL STOP, UTS-46 maps it to "."
        # The last two are why the pattern ends in \Z rather than $. Python's
        # $ also matches before a trailing newline, and .strip() runs before
        # rstrip("/"), so the slash shields the newline from the strip. Not
        # exploitable (urlsplit deletes the newline, httpx refuses the URL
        # outright), but it is the one character that reached a validated
        # result while being outside the charset, which is the whole argument.
        "evil.com\n/",
        "evil.com\r/",
    ]
    for planted in escapes:
        with pytest.raises(CredentialDestinationRefused):
            vendor_host(
                planted, vendor_suffix=".service-now.com",
                provider="servicenow", field="instance",
            )

    # Values that are NOT escapes must still be accepted, and the reason is that
    # they stay under the vendor: refusing them would be capability lost for no
    # security gained. Both were measured during the finding.
    for benign in ["evil.com/", "foo.bar", "https://evil.com"]:
        host = urlparse(
            "https://"
            + vendor_host(
                benign, vendor_suffix=".service-now.com",
                provider="servicenow", field="instance",
            )
            + "/api/now"
        ).netloc
        assert host.endswith(".service-now.com"), host


def test_the_refusal_does_not_echo_the_value():
    """One call site derives the fragment from the API KEY itself.

    ``customer_engagement``'s mailchimp reads its server prefix as
    ``api_key.rsplit("-", 1)[-1]``, so echoing a refused value would put a slice
    of the credential into a tool-visible error string, in the one corpus whose
    subject is credentials not reaching places they should not.
    """
    from nymeria.tools.native_credentials import CredentialDestinationRefused
    from nymeria.tools.service_integration_base import vendor_host

    with pytest.raises(CredentialDestinationRefused) as excinfo:
        vendor_host(
            "us21-abc123secret/x", vendor_suffix=".api.mailchimp.com",
            provider="mailchimp", field="server_prefix",
        )
    assert "abc123secret" not in str(excinfo.value)


def test_legitimate_tenants_still_resolve():
    """The helper returns the WHOLE host, which is what lets the gate below be
    a simple rule: no URL template in this corpus contains a vendor domain."""
    from nymeria.tools.service_integration_base import vendor_host

    for given, expected in [
        ("acme", "acme.service-now.com"),
        ("acme.service-now.com", "acme.service-now.com"),   # user pasted the host
        ("https://acme.service-now.com", "acme.service-now.com"),  # ...or the URL
        ("https://acme.service-now.com/", "acme.service-now.com"),
        ("ACME.SERVICE-NOW.COM", "ACME.service-now.com"),   # DNS is case-insensitive
        ("  acme  ", "acme.service-now.com"),
        ("my-tenant-1", "my-tenant-1.service-now.com"),
        ("foo.bar", "foo.bar.service-now.com"),             # multi-label tenants
    ]:
        assert vendor_host(
            given, vendor_suffix=".service-now.com",
            provider="servicenow", field="instance",
        ) == expected


# The marker that opts a site out, matched against the contiguous comment block
# directly above it. A table keyed by (module, function) was tried first and
# replaced: the repo already learned this one. test_subprocess_env_gate.py
# records that its own path:line table "broke twice in a single pass", and the
# join gate one function up already uses a call-site marker. Two things decided
# it here. A count of allowed sites per function cannot tell one exempt site
# from another, so deleting the okta dotted arm and adding a DIFFERENT
# unconstrained template in the same function kept the count at 1 and passed.
# And the reason ended up written twice, once at the site and once in the table,
# which is two copies of a security argument free to drift apart.
_AUTHORITY_MARKER = "authority-gate: not-a-fragment"

# The helper, by its real name and by the alias every integration imports it
# under. An earlier version asked ``"vendor_host" in _call_name(...)``, and a
# substring test is satisfied by ``_unsafe_vendor_host_passthrough``, so the one
# thing the gate checks could be defeated by naming a function.
_VENDOR_HOST_NAMES = frozenset({"vendor_host", "_vendor_host"})


def _is_vendor_host(call) -> bool:
    return (_call_name(call) or "") in _VENDOR_HOST_NAMES


def _authority_interpolations(tree, module_constants):
    """Yield ``(lineno, expr, kind, following)`` for authority interpolations.

    ``following`` is the literal text after the interpolation, with ``\x00``
    standing in for any later placeholder, so the caller can check that nothing
    trails a completed host inside the authority.

    Covers four interpolation forms, not just f-strings. That is not
    thoroughness for its own sake: ``sales_crm_service_integrations`` keeps its
    template in a module-level ``.format()`` constant, which is an
    ``ast.Constant`` and never an ``ast.JoinedStr`` no matter how wide the walk,
    so an f-string-only gate was measured blind to a real site. ``%``, ``+`` and
    ``"".join`` are covered because they are the same defect in a different
    spelling, and a ratchet that only recognises today's spelling stops being a
    ratchet the first time someone reformats.

    "Authority" means: after a literal ``http://``/``https://`` and before the
    first ``/`` that follows it. A name in the PATH cannot move the netloc, so
    counting one would report correct sites (chargebee interpolates its API
    version there).
    """
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            seen = ""
            hits = []
            for index, part in enumerate(node.values):
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    seen += part.value
                elif isinstance(part, ast.FormattedValue):
                    if _AUTHORITY_TAIL.search(seen):
                        hits.append((index, part.value))
                    seen += "\x00"
            for index, expr in hits:
                out.append((node.lineno, expr, "f-string", _tail_text(node.values[index + 1:])))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            # ``"https://" + host + ".vendor.com/x"``. Flattened left-to-right,
            # which is how Python associates it, so the accumulated literal
            # prefix is well defined. No site in the corpus spells a URL this
            # way today; it is here because it is ordinary Python and the
            # near-neighbour ``f"https://{root}"`` already appears, so it is the
            # spelling a future edit is most likely to reach for.
            for lineno, expr, following in _concat_authority(_flatten_add(node), node.lineno):
                out.append((lineno, expr, "+", following))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and isinstance(node.func.value, ast.Constant)
            and node.func.value.value == ""
            and node.args
            and isinstance(node.args[0], (ast.List, ast.Tuple))
        ):
            # ``"".join(["https://", host, ".vendor.com/x"])``, the same defect
            # spelled as a join, and forward-looking for the same reason as the
            # arm above. Only the empty-separator literal-sequence form is
            # understood; anything else is out of scope and said so below.
            for lineno, expr, following in _concat_authority(node.args[0].elts, node.lineno):
                out.append((lineno, expr, "join", following))
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
        ):
            segments = re.split(r"%(?:\([^)]*\))?[-+ #0]*\d*(?:\.\d+)?[sdr]", node.left.value)
            args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            seen = ""
            for index, segment in enumerate(segments[:-1]):
                seen += segment
                if _AUTHORITY_TAIL.search(seen):
                    out.append((
                        node.lineno,
                        args[index] if index < len(args) else node.right,
                        "%",
                        "\x00".join(segments[index + 1:]),
                    ))
                seen += "\x00"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
        ):
            target = node.func.value
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                template = target.value
            elif isinstance(target, ast.Name):
                template = module_constants.get(target.id)
            else:
                template = None
            if template is None:
                continue
            seen = ""
            cursor = 0
            for match in re.finditer(r"\{([^}:!]*)[^}]*\}", template):
                seen += template[cursor:match.start()]
                cursor = match.end()
                if _AUTHORITY_TAIL.search(seen):
                    key = match.group(1)
                    expr = None
                    if key.isdigit() or not key:
                        index = int(key or 0)
                        if index < len(node.args):
                            expr = node.args[index]
                    else:
                        expr = next(
                            (kw.value for kw in node.keywords if kw.arg == key), None
                        )
                    out.append((node.lineno, expr, ".format", _format_tail(template[cursor:])))
                seen += "\x00"
    return out


_AUTHORITY_TAIL = re.compile(r"https?://[^/\s]*$", re.IGNORECASE)


def _flatten_add(node):
    """Left-to-right operands of a chain of ``+``, so ``a + b + c`` is 3 not 2."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten_add(node.left) + _flatten_add(node.right)
    return [node]


def _tail_text(parts) -> str:
    """Literal text of the remaining f-string parts, placeholders as NUL."""
    return "".join(
        part.value
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
        else "\x00"
        for part in parts
    )


def _format_tail(rest: str) -> str:
    return re.sub(r"\{[^}]*\}", "\x00", rest)


def _concat_authority(parts, lineno):
    """Operands of a string concatenation that land in a URL authority."""
    found = []
    seen = ""
    for index, part in enumerate(parts):
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            seen += part.value
            continue
        if _AUTHORITY_TAIL.search(seen):
            found.append((getattr(part, "lineno", lineno), part, _tail_text(parts[index + 1:])))
        seen += "\x00"
    return found


def test_every_url_authority_in_tools_is_built_by_the_helper():
    """The ratchet, as an opt-OUT over every module in ``nymeria/tools/``.

    The first version of this gate was an opt-IN keyed on a hand list of vendor
    suffixes, and adversarial mutation measured it catching ONE of the nine real
    sites: deleting the helper call at freshworks_crm or bubble passed clean, and
    so did a whole new module with an unconstrained template. Two lessons, both
    of them structural rather than about that particular list:

    1. An opt-in gate cannot see a module nobody added to it, which is exactly
       the case it exists for. So this walks every module and requires an
       exemption to be DECLARED, with its reason, in a marker comment at
       the site itself.
    2. Keying on the vendor suffix appearing in the template only works while
       the template contains the suffix. ``vendor_host`` owns the suffix, so a
       compliant template has no vendor domain in it at all. The rule here is
       the inverse and does not depend on recognising a vendor: ANY
       interpolation into a URL authority must be a call to the helper, or a
       local this function assigned from one.

    It is deliberately DOUBLED by ``test_every_declared_host_fragment_reaches_
    the_helper`` at the end of this file, which asks the same question from the
    credential register instead of from the source text. Each sees a class the
    other cannot, and without that one this parser's admitted blind spots below
    would be blind spots for the whole control.

    Deleting any of the twelve helper calls now fails this test and names the
    line, as does adding an unconstrained template to a new module, to a listed
    module, or inside a function that already holds an exemption.

    WHAT IT STILL CANNOT SEE, both measured by mutation rather than reasoned
    about, and both left open deliberately because closing them costs more than
    the shape is worth:

    * A scheme that is not a literal. ``proto = "https"`` then
      ``f"{proto}://{host}.vendor.com"`` reads the accumulated prefix as having
      no scheme, so no interpolation looks like an authority. Catching it means
      constant-folding locals, which is most of an interpreter; nothing in this
      corpus writes a URL that way.
    * A name REBOUND after the helper assigned it. ``constrained`` is the set of
      names this function ever assigned from ``vendor_host``, not a flow
      analysis, so ``host = vendor_host(...)`` followed later by
      ``host = something_else`` still counts as constrained. This is the same
      trade the sibling join gate makes one function over, and the same answer:
      a name-based approximation catches deletion, which is the change a
      refactor actually makes, and misses deliberate rebinding, which is not.

    Read a green run as "no new unconstrained authority of a shape this gate
    recognises", not as "no template can be steered".
    """
    problems = []
    marked: set[tuple[str, int]] = set()
    for path in _tools_modules():
        source = path.read_text()
        source_lines = source.splitlines()
        tree = ast.parse(source)
        # ``AnnAssign`` as well as ``Assign``. Measured: annotating the
        # freshworks template (``_FRESHWORKS_CRM_BASE_URL: str = "..."``, which
        # is idiomatic in this repo, see credential_registry) and deleting its
        # helper call passed the gate green with the original vulnerability
        # fully restored. One token was the whole difference.
        module_constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = [t for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target]
            else:
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for target in targets:
                    module_constants[target.id] = node.value.value
        # Attribute each site to the innermost function containing it, so the
        # "assigned from the helper" lookup below sees that function's own
        # locals rather than an enclosing scope's.
        owners = {}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            constrained = {
                target.id
                for node in ast.walk(fn)
                if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and _is_vendor_host(node.value)
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            for lineno, expr, kind, following in _authority_interpolations(
                fn, module_constants
            ):
                key = (lineno, kind, ast.dump(expr) if expr else "", following)
                previous = owners.get(key)
                if previous is None or fn.lineno > previous[0].lineno:
                    owners[key] = (fn, expr, constrained, kind, lineno, following)
        for fn, expr, constrained, kind, lineno, following in owners.values():
            # Two accepted shapes and no others. An arbitrary expression whose
            # NAMES are all constrained is not the same as a constrained value:
            # ``f"https://{host.replace('vendor.com', 'evil.com')}"`` passed a
            # ``names <= constrained`` test while rebuilding the whole defect.
            constrained_here = (
                isinstance(expr, ast.Call) and _is_vendor_host(expr)
            ) or (isinstance(expr, ast.Name) and expr.id in constrained)
            if constrained_here:
                # ...and nothing may follow it inside the authority. Owning the
                # suffix in the helper is what makes this gate vendor-agnostic,
                # and that only holds while no template appends a domain of its
                # own afterwards: f"https://{vendor_host(...)}.evil.com" is
                # otherwise a fully constrained call landing on evil.com.
                trailing = following.split("\x00", 1)[0]
                head = trailing.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
                if head:
                    problems.append(
                        f"{path.name}:{lineno} ({fn.name}, {kind}) appends "
                        f"{head!r} to a host the helper already completed, which "
                        "puts a vendor domain back into a URL template and "
                        "moves the netloc off whatever the helper returned"
                    )
                continue
            # Only AFTER the two "this site is constrained" arms, so a marker
            # is never what makes a correctly built site pass.
            marker = _markers_above(source_lines, lineno)
            if _AUTHORITY_MARKER in marker:
                marked.add((path.name, lineno))
                # Length of the whole comment block, not of what follows the
                # marker on its own line: _markers_above collects upward, so the
                # block arrives bottom-first and the marker is usually its last
                # element with the reason ABOVE it in the joined string.
                if len(marker) - len(_AUTHORITY_MARKER) < 80:
                    problems.append(
                        f"{path.name}:{lineno} ({fn.name}) carries the marker "
                        "with no reason around it. The marker is meant to be "
                        "read by a person reviewing the site, so a bare one is "
                        "worse than none: it silences the gate and explains "
                        "nothing"
                    )
                continue
            problems.append(
                f"{path.name}:{lineno} ({fn.name}, {kind}) interpolates into a "
                "URL authority without vendor_host, so the value can terminate "
                "the authority and move the netloc off the intended host"
            )
    assert not problems, (
        "unconstrained URL authority interpolations:\n  "
        + "\n  ".join(problems)
        + "\n\nEither build the host with service_integration_base.vendor_host, "
        f"or put a `# {_AUTHORITY_MARKER} - <reason>` comment directly above the "
        "site saying which other control governs it."
    )
    # A marker cannot go stale (delete the line and it goes with it), so there
    # is no staleness assert to write. What is worth pinning is that markers
    # stay RARE: this gate is only as strong as the number of sites that have
    # opted out of it, and three is small enough to re-read on every change.
    assert len(marked) == 2, (
        f"{len(marked)} sites now carry the {_AUTHORITY_MARKER!r} marker "
        f"({sorted(marked)}), not 2. "
        "Adding one is allowed and sometimes right, but it is the one edit that "
        "makes this gate weaker, so it should be a deliberate line in a diff "
        "rather than a number that drifts. Update this count with the site."
    )


@pytest.mark.parametrize(
    "field,planted",
    [("base_url", "https://attacker.invalid"), ("domain", "attacker.invalid")],
)
def test_okta_is_joined_a_layer_down_rather_than_at_the_site(
    vault_setup, monkeypatch, field, planted
):
    """Evidence for the two ``_okta_*`` markers in enrichment_security.

    They claim okta needs no site guard because a record that supplies the
    address must also hold the token. That is anchor ARITHMETIC, not a control:
    it holds because okta declares exactly ONE anchor group, so slice B's
    refusal fires at the LOOKUP, before this module sees a value.

    So the PREMISE is asserted directly, and that is the load-bearing half.
    An earlier version of this test asserted only the consequence and claimed
    in its own docstring that it "fails the moment the arithmetic changes";
    review added a second anchor group to the spec, built the record the
    docstring described, watched the operator's env token ride to the planted
    address, and this test still passed. The consequence is asserted too, but
    it is the cheap half: the pre-existing join gate already covers it.

    Note the refusal comes from ``native_credentials``, not from any call in
    ``_okta_config``: reading the site alone would suggest it is unguarded.
    """
    from nymeria.tools import enrichment_security_service_integrations as tools
    from nymeria.tools.credential_registry import credential_anchor_fields
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    # The premise. If this ever fails, the marker at _okta_config is no longer
    # true and that site needs a guard of its own: with two anchor groups a
    # record can hold credential B, supply the address, and the operator's
    # env-configured credential A rides to it.
    anchors = [
        group
        for group in tools._OKTA.groups
        if group.names[0] in credential_anchor_fields(tools._OKTA)
    ]
    assert len(anchors) == 1, (
        f"okta now declares {len(anchors)} anchor groups, not 1: "
        f"{[g.names[0] for g in anchors]}. The exemption marker at "
        "_okta_config rests on there being exactly one."
    )

    _mint(vault_setup, name=f"planted-{field}", provider="okta",
          secret_fields={field: planted})
    monkeypatch.setattr(
        tools,
        "_settings_value",
        lambda name: "OPERATOR-SSWS-TOKEN" if name == "okta_access_token" else None,
    )

    with pytest.raises(CredentialDestinationRefused) as excinfo:
        tools._okta_config("okta_list_users", {"configurable": {"user_id": "alice"}})
    assert field in str(excinfo.value)


@pytest.mark.parametrize("planted", ["internal#", "internal?x", "internal:8080#"])
def test_the_okta_dotless_arm_keeps_its_own_vendor_promise(
    vault_setup, monkeypatch, planted
):
    """The escape review measured, on a record that holds BOTH halves.

    The test above shows a record supplying only the address is refused at the
    lookup, which is what makes okta safe from credential EGRESS. It says
    nothing about the fragment, because that record never reaches this code. A
    record that also holds the token does reach it, and before the dotless arm
    called vendor_host it built "https://internal#.okta.com", whose netloc is
    "internal": the vendor suffix promised by the template ends up in the URL
    fragment. None of these three values contains a dot, which is exactly why
    "it must be dotted to be dangerous" was the wrong reading.

    The remaining reach is then that record's OWN credential to a non-okta
    host, which the egress policy screens by resolved address. Small, but the
    template made a promise and this is what keeping it looks like.
    """
    from urllib.parse import urlparse

    from nymeria.tools import enrichment_security_service_integrations as tools
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    _mint(
        vault_setup,
        name="self-consistent",
        provider="okta",
        secret_fields={"domain": planted, "api_token": "record-own-token"},
    )
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    with pytest.raises(CredentialDestinationRefused):
        tools._okta_config("okta_list_users", {"configurable": {"user_id": "alice"}})

    # ...and the legitimate spelling of the same field still works, so this is
    # not the charset refusing everything.
    assert urlparse(
        "https://"
        + base.vendor_host("acme", vendor_suffix=".okta.com",
                           provider="okta", field="domain")
    ).netloc == "acme.okta.com"


def test_every_declared_host_fragment_reaches_the_helper():
    """The same rule again, derived from the REGISTER rather than from an AST parse.

    Deliberately redundant with the gate above, and the redundancy is the point.
    That one walks URL templates, so it sees a site whose value came from a
    setting no spec declares, and it is blind to a template spelling it does not
    recognise. This one walks the register, so it sees every field the register
    calls a host fragment no matter how the URL is spelled, and it is blind to a
    fragment no spec declares. Each covers a class the other cannot, and without
    this one the AST parser's admitted blind spots would be blind spots for the
    whole control.

    The scope comes from ``credential_registry.destination_host_fragment_fields_for``,
    per spec, for the reason its docstring gives: a NAME set is right per name
    and wrong per provider. Measured here, not hypothetically: magento's
    destination group has the primary ``host``, which is a fragment name at other
    providers, so intersecting NAMES rather than matching GROUPS reported
    magento's whole-URL lookup as a fragment and would have demanded the wrong
    control at it.
    """
    from nymeria.tools.credential_registry import (
        DESTINATION_HOST_FRAGMENT_FIELDS,
        destination_host_fragment_fields_for,
    )

    problems = []
    reaching = 0
    marked = 0
    for path in _tools_modules():
        if path.name == "__init__.py":
            continue
        module = importlib.import_module(f"nymeria.tools.{path.stem}")
        source = path.read_text()
        source_lines = source.splitlines()
        tree = ast.parse(source)
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        wrappers = _lookup_wrappers(functions, module)
        for fn in functions:
            # Names that reach the helper, closed backwards over assignment.
            # The lookup rarely feeds vendor_host directly: the corpus writes
            # ``subdomain = subdomain_from_vault or _settings_value(...)`` and
            # then hands ``subdomain`` over, and okta puts a ``.strip()`` in
            # between, so a check for the lookup's own target reports four
            # correct sites. This walks assignments to a fixpoint: if a name
            # reaches the helper, every name its value was derived from does.
            #
            # It over-approximates, by design. ``x = a or b`` followed by
            # ``vendor_host(x)`` marks BOTH a and b as reaching, so a function
            # that constrains one arm of an alternation and not the other reads
            # as clean here. That is the same name-based approximation the gate
            # above makes, and the same answer applies: this rule exists to
            # catch a fragment with no constraint anywhere, and the per-arm
            # question belongs to the join gate, which is already per branch.
            to_helper = set()
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and _is_vendor_host(node):
                    for inner in ast.walk(node):
                        if isinstance(inner, ast.Name):
                            to_helper.add(inner.id)
            derived_from = {}
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                    continue
                if not isinstance(node.targets[0], ast.Name):
                    continue
                sources = {
                    inner.id
                    for inner in ast.walk(node.value)
                    if isinstance(inner, ast.Name)
                }
                derived_from.setdefault(node.targets[0].id, set()).update(sources)
            changed = True
            while changed:
                changed = False
                for name in list(to_helper):
                    for source in derived_from.get(name, ()):
                        if source not in to_helper:
                            to_helper.add(source)
                            changed = True
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                    continue
                target = node.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                # EVERY operand of an ``or`` chain, not just the first. aws
                # writes ``region = region_name.strip() or _credential_value(...)
                # or _settings_value(...)``, so taking values[0] silently drops
                # the one lookup in the corpus that is not written first. That
                # is the same shape as ratchet hole 1 in the sibling join gate.
                operands = (
                    node.value.values
                    if isinstance(node.value, ast.BoolOp)
                    else [node.value]
                )
                call = next(
                    (
                        operand
                        for operand in operands
                        if isinstance(operand, ast.Call)
                        and (_call_name(operand) or "").endswith("credential_value")
                        or (
                            isinstance(operand, ast.Call)
                            and _call_name(operand) in wrappers
                        )
                    ),
                    None,
                )
                if call is None:
                    continue
                keywords = {kw.arg: kw.value for kw in call.keywords or []}
                spec = _resolved_spec(keywords.get("provider"), module)
                if spec is None:
                    spec = wrappers.get(_call_name(call))
                fields = _resolved_field_names(keywords.get("field_names"), module)
                if spec is None or fields is None:
                    continue
                # Two conditions, and both are needed. The register accessor
                # answers "is this NAME a fragment name for this provider",
                # which is the per-spec half. GROUP identity answers "is this
                # LOOKUP asking for the fragment group", which the accessor
                # cannot: magento's whole-URL group carries `host` and `url`
                # aliases that are fragment primaries elsewhere, so a name test
                # alone reported its address lookup as a fragment.
                fragment_names = destination_host_fragment_fields_for(spec)
                if not (fields & fragment_names):
                    continue
                if not any(
                    group.names[0] in DESTINATION_HOST_FRAGMENT_FIELDS
                    and fields == frozenset(group.names)
                    for group in spec.groups
                ):
                    continue
                if target.id in to_helper:
                    reaching += 1
                    continue
                # Above the ASSIGNMENT, not above the call: the corpus wraps
                # these lookups in a parenthesised ``x = (\n  lookup(...)\n  or
                # ...)``, so the call's own line has ``x = (`` above it and a
                # marker there would never be found.
                marker = _markers_above(source_lines, node.lineno) or _markers_above(
                    source_lines, call.lineno
                )
                if _AUTHORITY_MARKER in marker:
                    marked += 1
                    # Same standard as the gate above, and it was missing here
                    # until mutation testing hollowed out s3's reason to a bare
                    # marker and this rule passed. A marker with no reason is
                    # the worst of both: it silences the check and records no
                    # judgement for the next reader to disagree with.
                    if len(marker) - len(_AUTHORITY_MARKER) < 80:
                        problems.append(
                            f"{path.name}:{call.lineno} ({fn.name}) carries the "
                            "marker with no reason around it"
                        )
                    continue
                problems.append(
                    f"{path.name}:{call.lineno} ({fn.name}) resolves "
                    f"{spec.provider}'s declared host FRAGMENT into "
                    f"{target.id!r}, which never reaches vendor_host"
                )
    assert not problems, (
        "declared host fragments with no constraint:\n  "
        + "\n  ".join(problems)
        + "\n\nEither build the host with service_integration_base.vendor_host, "
        f"or put a `# {_AUTHORITY_MARKER} - <reason>` comment above the lookup "
        "saying what the value actually does (a path segment, a whole address, "
        "a switch between constants, or a third party's own validation)."
    )
    assert (reaching, marked) == (13, 7), (
        f"the register declares {reaching + marked} host-fragment lookups, of "
        f"which {reaching} reach vendor_host and {marked} are marked; expected "
        "13 and 7. A rise in the marked count is the one that matters: it means "
        "a fragment was classified as needing no constraint, which is the "
        "judgement this whole finding turned on getting wrong once."
    )


@pytest.mark.parametrize("planted", ["evil.example.net/x#", "evil.example.net?", "evil.example.net:8443#"])
def test_the_erpnext_default_domain_is_a_vendor_promise_too(vault_setup, monkeypatch, planted):
    """The site this pass first exempted on a reason that was false by default.

    erpnext composes ``f"https://{subdomain}.{cloud_domain}"``, and the first
    reading called both halves free text, so no vendor domain was hard-coded and
    there was nothing to escape. But ``erpnext_cloud_domain`` is a Settings field
    with the default ``"erpnext.com"``, so on an install that has not set it the
    template is exactly the vendor shape this finding is about. Review measured
    the leak with a self-consistent record, which is the shape that clears the
    join gate: netloc ``evil.example.net`` carrying the record's own token.

    The lesson is in the test name. A DEFAULT is part of the template. "Free
    text" described the field's type, and what mattered was its value on a box
    nobody configured.
    """
    from nymeria.tools import enterprise_business_service_integrations as tools
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    _mint(
        vault_setup,
        name="self-consistent",
        provider="erpnext",
        secret_fields={
            "subdomain": planted,
            "api_key": "record-own-key",
            "api_secret": "record-own-secret",
        },
    )
    # The default is the POINT, so it is left in place rather than patched
    # away: an earlier draft of this test stubbed _settings_value to None, which
    # left cloud_domain empty and skipped the composing branch entirely, so it
    # passed while testing nothing.
    monkeypatch.setattr(
        tools,
        "_settings_value",
        lambda name: "erpnext.com" if name == "erpnext_cloud_domain" else None,
    )

    with pytest.raises(CredentialDestinationRefused):
        tools._erpnext_config("erpnext_list_records", {"configurable": {"user_id": "alice"}})


def test_a_vendor_suffix_that_is_itself_a_destination_is_refused():
    """The suffix is checked, and at erpnext it is DATA rather than a literal.

    Without this, the second half of a composed host does exactly what the
    constrainer stops the first half doing, and the three degenerate literal
    spellings below quietly return a host outside the vendor from a function
    whose entire promise is the opposite.
    """
    from nymeria.tools.native_credentials import CredentialDestinationRefused

    for suffix in ["", "okta.com", "@evil.com", ".evil.com/x", "."]:
        with pytest.raises(CredentialDestinationRefused):
            base.vendor_host(
                "acme", vendor_suffix=suffix, provider="probe", field="tenant"
            )

    assert base.vendor_host(
        "acme", vendor_suffix=".okta.com", provider="probe", field="tenant"
    ) == "acme.okta.com"



def test_every_join_gate_exemption_marker_carries_a_reason():
    """A bare marker is an opt-out with nothing to review.

    Both sibling gates carry this test (``test_subprocess_env_gate.py``,
    ``test_exec_sandbox_gate.py``, each rejecting a reason under 20 characters)
    and this one did not, which mattered more here than it looks: the whole
    argument for putting exemptions at the call site rather than in a table is
    that the next reader finds the justification where the code is. An
    unexplained marker gives that up and keeps the opt-out.

    Audit task #69 more than doubled the marker count, from 3 to 7, by reverting
    a resolver widening in favour of marking the four helpers it had been widened
    to resolve. That is a defensible trade only if each marker says why, so the
    test arrives with the markers rather than after them.

    Scoped to the marker LINE, not the block above it, unlike the sibling gates.
    Those anchor to a single spawn; this marker is scoped to a whole function
    body (the join spans two statements, so there is no one line to sit above),
    which means a block-scoped reading would count any nearby comment as the
    reason.
    """
    unexplained = []
    for path in _tools_modules():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.lstrip()
            if not stripped.startswith("#") or _ENFORCED_ELSEWHERE not in stripped:
                continue
            reason = stripped.split(_ENFORCED_ELSEWHERE, 1)[1].lstrip("#-: ").strip()
            if len(reason) < 20:
                unexplained.append(f"{path.name}:{number}")
    assert not unexplained, (
        "These join-gate exemption markers have no usable reason:\n  "
        + "\n  ".join(unexplained)
    )
