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
thing a later change removes without noticing:

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
