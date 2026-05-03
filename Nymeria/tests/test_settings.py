from nymeria.config.settings import DEFAULT_CORS_ORIGINS, Settings


def test_cors_default_is_restricted_to_local_desktop_origins():
    assert Settings.model_fields["cors_origins"].default == DEFAULT_CORS_ORIGINS
    assert DEFAULT_CORS_ORIGINS != "*"

    settings = Settings(_env_file=None, cors_origins=DEFAULT_CORS_ORIGINS)

    assert settings.cors_origins_list == [
        "http://localhost:1420",
        "tauri://localhost",
    ]


def test_cors_wildcard_requires_explicit_override():
    settings = Settings(_env_file=None, cors_origins="*")

    assert settings.cors_origins_list == ["*"]
