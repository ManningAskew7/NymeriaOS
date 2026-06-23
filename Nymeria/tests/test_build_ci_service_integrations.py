import json


from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo as _use_repo,
    make_vault_repo as _repo,
)


def test_circleci_trigger_pipeline_uses_env_token(monkeypatch):
    from nymeria.tools import build_ci_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("CIRCLECI_API_TOKEN", "circle-token")
    monkeypatch.setenv("CIRCLECI_BASE_URL", "https://circle.example/api/v2")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"id": "pipeline-id"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.circleci_trigger_pipeline.func(
            vcs="github",
            project_slug="org/repo",
            branch="main",
            parameters_json='{"deploy": true}',
        )
    )

    assert result["id"] == "pipeline-id"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://circle.example/api/v2/project/github/org%2Frepo/pipeline"
    assert captured["headers"]["Circle-Token"] == "circle-token"
    assert captured["json_body"] == {"branch": "main", "parameters": {"deploy": True}}


def test_travisci_trigger_build_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import build_ci_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Travis CI",
        provider="travis_ci",
        kind="api_key",
        allowed_targets=["native_tool:travisci_trigger_build"],
        secret_fields={"apiToken": "travis-token", "base_url": "https://travis.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"request": {"id": 12}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.travisci_trigger_build.func(
            slug="org/repo",
            branch="main",
            message="manual",
            merge_mode="deep_merge",
            config_json='{"env": {"FOO": "bar"}}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["request"]["id"] == 12
    assert captured["method"] == "POST"
    assert captured["url"] == "https://travis.example/repo/org%2Frepo/requests"
    assert captured["headers"]["Authorization"] == "token travis-token"
    assert captured["json_body"]["request"]["branch"] == "main"
    assert captured["json_body"]["request"]["config"] == {"env": {"FOO": "bar"}}


def test_jenkins_list_job_builds_uses_env_basic_auth(monkeypatch):
    from nymeria.tools import build_ci_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("JENKINS_BASE_URL", "https://jenkins.example/")
    monkeypatch.setenv("JENKINS_USERNAME", "ada")
    monkeypatch.setenv("JENKINS_API_TOKEN", "jenkins-token")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"builds": [{"number": 7, "result": "SUCCESS"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.jenkins_list_job_builds.func(job_name="folder/build", limit=10))

    assert result == [{"number": 7, "result": "SUCCESS"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://jenkins.example/job/folder/job/build/api/json"
    assert "builds[number" in captured["params"]["tree"]
    assert captured["auth"] is not None


def test_jenkins_parameterized_trigger_uses_vault(tmp_path, monkeypatch):
    from nymeria.tools import build_ci_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Jenkins",
        provider="jenkins",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={"baseUrl": "https://jenkins.example", "username": "ada", "apiKey": "token"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"status": "ok"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.jenkins_trigger_job_with_parameters.func(
            job_name="build",
            parameters_json='{"ENV": "prod", "RUN_TESTS": "true"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["status"] == "ok"
    assert captured["url"] == "https://jenkins.example/job/build/buildWithParameters"
    assert captured["form_data"] == {"ENV": "prod", "RUN_TESTS": "true"}
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_build_ci_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import build_ci_service_integrations as tools

    values = {"jenkins_base_url": "https://jenkins.example"}
    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: values.get(name))

    circleci = tools.circleci_get_pipeline.func(vcs="github", project_slug="org/repo", pipeline_number=1)
    travis = tools.travisci_get_build.func(build_id="123")
    jenkins = tools.jenkins_get_instance.func()

    assert "No CircleCI credential found" in circleci
    assert "CIRCLECI_API_TOKEN" in circleci
    assert "native_tool:circleci_get_pipeline" in circleci
    assert "No Travis CI credential found" in travis
    assert "TRAVISCI_API_TOKEN" in travis
    assert "No Jenkins credential found" in jenkins
    assert "JENKINS_USERNAME" in jenkins


def test_build_ci_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "circleci_list_pipelines",
        "circleci_get_pipeline",
        "travisci_list_builds",
        "travisci_get_build",
        "jenkins_get_instance",
        "jenkins_list_jobs",
        "jenkins_list_job_builds",
    }
    moderate_names = {
        "circleci_trigger_pipeline",
        "travisci_trigger_build",
        "travisci_restart_build",
        "travisci_cancel_build",
        "jenkins_trigger_job",
        "jenkins_trigger_job_with_parameters",
        "jenkins_copy_job",
        "jenkins_create_job",
        "jenkins_quiet_down",
        "jenkins_cancel_quiet_down",
        "jenkins_restart_instance",
        "jenkins_shutdown_instance",
    }

    for name in safe_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.SAFE

    for name in moderate_names:
        assert name in CATALOG_TOOLS
        metadata = get_tool_metadata(name)
        assert metadata is not None
        assert metadata.category == ToolCategory.INTEGRATIONS
        assert metadata.security_level == SecurityLevel.MODERATE


def test_build_ci_tool_schemas_hide_runtime_config():
    from nymeria.tools.build_ci_service_integrations import (
        circleci_trigger_pipeline,
        jenkins_trigger_job_with_parameters,
        travisci_trigger_build,
    )

    assert "config" not in circleci_trigger_pipeline.args_schema.model_json_schema()["properties"]
    assert "config" not in travisci_trigger_build.args_schema.model_json_schema()["properties"]
    assert "config" not in jenkins_trigger_job_with_parameters.args_schema.model_json_schema()["properties"]
