import base64
import json


from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo as _use_repo,
    make_vault_repo as _repo,
)


def test_activecampaign_list_contacts_uses_env_auth(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("ACTIVECAMPAIGN_API_KEY", "active-key")
    monkeypatch.setenv("ACTIVECAMPAIGN_BASE_URL", "https://acme.api-us1.com")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"contacts": [{"id": "1", "email": "ada@example.com"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.activecampaign_list_contacts.func(search="ada", limit=5))

    assert result["contacts"][0]["email"] == "ada@example.com"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://acme.api-us1.com/api/3/contacts"
    assert captured["params"]["search"] == "ada"
    assert captured["params"]["limit"] == 5
    assert captured["headers"]["Api-Token"] == "active-key"


def test_convertkit_subscribe_uses_vault_secret(tmp_path, monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="ConvertKit",
        provider="convertkit",
        kind="api_key",
        allowed_targets=["native_tool:convertkit_add_subscriber_to_form"],
        secret_fields={"apiSecret": "kit-secret", "baseUrl": "https://kit.example/v3"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"subscription": {"subscriber": {"email_address": kwargs["json_body"]["email"]}}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.convertkit_add_subscriber_to_form.func(
            form_id="form-1",
            email="ada@example.com",
            first_name="Ada",
            fields_json='{"role": "engineer"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["subscription"]["subscriber"]["email_address"] == "ada@example.com"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://kit.example/v3/forms/form-1/subscribe"
    assert captured["json_body"]["api_secret"] == "kit-secret"
    assert captured["json_body"]["fields"] == {"role": "engineer"}


def test_getresponse_missing_key_returns_setup_hint(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)

    result = tools.getresponse_list_campaigns.func()

    assert "No GetResponse credential found" in result
    assert "GETRESPONSE_API_KEY" in result
    assert "native_tool:getresponse_list_campaigns" in result


def test_mailerlite_create_subscriber_uses_classic_header(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("MAILERLITE_API_KEY", "lite-key")
    monkeypatch.setenv("MAILERLITE_CLASSIC_API", "true")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"data": {"email": kwargs["json_body"]["email"]}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mailerlite_create_subscriber.func(
            email="ada@example.com",
            name="Ada",
            fields_json='{"company": "Analytical Engines"}',
            groups="g1,g2",
        )
    )

    assert result["data"]["email"] == "ada@example.com"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.mailerlite.com/api/v2/subscribers"
    assert captured["headers"]["X-MailerLite-ApiKey"] == "lite-key"
    assert captured["json_body"]["groups"] == ["g1", "g2"]


def test_customerio_uses_app_and_tracking_auth(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("CUSTOMERIO_APP_API_KEY", "app-key")
    monkeypatch.setenv("CUSTOMERIO_TRACKING_SITE_ID", "site-1")
    monkeypatch.setenv("CUSTOMERIO_TRACKING_API_KEY", "track-key")
    captured = []

    def fake_request(method, url, **kwargs):
        captured.append({"method": method, "url": url, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    assert json.loads(tools.customerio_list_campaigns.func())["ok"] is True
    assert json.loads(tools.customerio_track_event.func(customer_id="cust-1", event_name="signed_up"))["ok"] is True

    assert captured[0]["method"] == "GET"
    assert captured[0]["url"] == "https://api.customer.io/v1/campaigns"
    assert captured[0]["headers"]["Authorization"] == "Bearer app-key"
    assert captured[1]["method"] == "POST"
    assert captured[1]["url"] == "https://track.customer.io/api/v1/customers/cust-1/events"
    expected_basic = base64.b64encode(b"site-1:track-key").decode()
    assert captured[1]["headers"]["Authorization"] == f"Basic {expected_basic}"
    assert captured[1]["json_body"]["name"] == "signed_up"


def test_iterable_track_event_uses_bulk_endpoint(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("ITERABLE_API_KEY", "iter-key")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"code": "Success"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.iterable_track_event.func(
            event_name="signed_up",
            email="ada@example.com",
            data_fields_json='{"plan": "pro"}',
            campaign_id=42,
        )
    )

    assert result["code"] == "Success"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.iterable.com/api/events/trackBulk"
    assert captured["headers"]["Api_Key"] == "iter-key"
    assert captured["json_body"]["events"][0]["email"] == "ada@example.com"
    assert captured["json_body"]["events"][0]["dataFields"] == {"plan": "pro"}
    assert captured["json_body"]["events"][0]["campaignId"] == 42


def test_posthog_capture_event_includes_project_key(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("POSTHOG_API_KEY", "ph-key")
    monkeypatch.setenv("POSTHOG_BASE_URL", "https://posthog.example")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"status": "ok"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.posthog_capture_event.func(
            event_name="signed_up",
            distinct_id="user-1",
            properties_json='{"plan": "pro"}',
        )
    )

    assert result["status"] == "ok"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://posthog.example/capture"
    assert captured["json_body"]["api_key"] == "ph-key"
    assert captured["json_body"]["properties"]["distinct_id"] == "user-1"
    assert captured["json_body"]["properties"]["plan"] == "pro"


def test_segment_identify_uses_basic_write_key(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("SEGMENT_WRITE_KEY", "seg-write")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"success": True}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.segment_identify.func(
            user_id="user-1",
            traits_json='{"email": "ada@example.com"}',
        )
    )

    assert result["success"] is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.segment.io/v1/identify"
    expected_basic = base64.b64encode(b"seg-write:").decode()
    assert captured["headers"]["Authorization"] == f"Basic {expected_basic}"
    assert captured["json_body"]["userId"] == "user-1"
    assert captured["json_body"]["traits"]["email"] == "ada@example.com"


def test_lemlist_create_lead_uses_vault_basic_auth(tmp_path, monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Lemlist",
        provider="lemlist",
        kind="api_key",
        allowed_targets=["native_tool:lemlist_create_lead"],
        secret_fields={"apiKey": "lem-key", "base_url": "https://lem.example/api"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"email": "ada@example.com", "campaignId": "camp-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.lemlist_create_lead.func(
            campaign_id="camp-1",
            email="ada@example.com",
            fields_json='{"firstName":"Ada"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["email"] == "ada@example.com"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://lem.example/api/campaigns/camp-1/leads/ada%40example.com"
    assert captured["json_body"] == {"firstName": "Ada"}
    assert captured["params"] == {"deduplicate": True}
    expected_basic = base64.b64encode(b":lem-key").decode()
    assert captured["headers"]["Authorization"] == f"Basic {expected_basic}"


def test_sendy_status_uses_form_key_and_url(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("SENDY_URL", "https://sendy.example")
    monkeypatch.setenv("SENDY_API_KEY", "sendy-key")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return "Subscribed"

    monkeypatch.setattr(tools, "_request_any", fake_request)

    result = json.loads(tools.sendy_get_subscriber_status.func(email="ada@example.com", list_id="list-1"))

    assert result == {"status": "Subscribed"}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://sendy.example/api/subscribers/subscription-status.php"
    assert captured["data"]["email"] == "ada@example.com"
    assert captured["data"]["list_id"] == "list-1"
    assert captured["data"]["api_key"] == "sendy-key"
    assert captured["data"]["boolean"] == "true"


def test_emelia_create_campaign_uses_graphql_auth(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("EMELIA_API_KEY", "emelia-key")
    monkeypatch.setenv("EMELIA_GRAPHQL_URL", "https://emelia.example/graphql")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"data": {"createCampaign": {"_id": "camp-1", "name": "Outbound"}}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.emelia_create_campaign.func(name="Outbound"))

    assert result == {"_id": "camp-1", "name": "Outbound"}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://emelia.example/graphql"
    assert captured["headers"]["Authorization"] == "emelia-key"
    assert captured["json_body"]["operationName"] == "createCampaign"
    assert captured["json_body"]["variables"] == {"name": "Outbound"}


def test_marketing_new_services_return_setup_hints(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    assert "LEMLIST_API_KEY" in tools.lemlist_list_campaigns.func()
    assert "SENDY_URL" in tools.sendy_get_subscriber_status.func(email="ada@example.com", list_id="list-1")
    assert "EMELIA_API_KEY" in tools.emelia_list_campaigns.func()


def test_actionnetwork_create_person_uses_osdi_token(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("ACTIONNETWORK_API_KEY", "action-key")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"_links": {"self": {"href": "https://actionnetwork.org/api/v2/people/person-1"}}}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.actionnetwork_create_person.func(
            email="ada@example.com",
            given_name="Ada",
            family_name="Lovelace",
            fields_json='{"languages_spoken": ["en"]}',
        )
    )

    assert result["_links"]["self"]["href"].endswith("/person-1")
    assert captured["method"] == "POST"
    assert captured["url"] == "https://actionnetwork.org/api/v2/people"
    assert captured["headers"]["OSDI-API-Token"] == "action-key"
    assert captured["json_body"]["person"]["email_addresses"][0]["address"] == "ada@example.com"
    assert captured["json_body"]["person"]["given_name"] == "Ada"
    assert captured["json_body"]["person"]["languages_spoken"] == ["en"]


def test_autopilot_upsert_contact_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Autopilot",
        provider="autopilot",
        kind="api_key",
        allowed_targets=["native_tool:autopilot_upsert_contact"],
        secret_fields={"apiKey": "auto-key", "baseUrl": "https://auto.example/v1"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"contact_id": "contact-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.autopilot_upsert_contact.func(
            email="ada@example.com",
            fields_json='{"FirstName": "Ada"}',
            list_id="list-1",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["contact_id"] == "contact-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://auto.example/v1/contact"
    assert captured["headers"]["autopilotapikey"] == "auto-key"
    assert captured["json_body"]["contact"]["Email"] == "ada@example.com"
    assert captured["json_body"]["contact"]["_autopilot_list"] == "list-1"


def test_egoi_create_contact_attaches_tags(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("EGOI_API_KEY", "egoi-key")
    captured = []

    def fake_request(method, url, **kwargs):
        captured.append({"method": method, "url": url, **kwargs})
        if url.endswith("/contacts"):
            return {"contact_id": "contact-1"}
        return {"ok": True}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.egoi_create_contact.func(
            list_id="list-1",
            email="ada@example.com",
            fields_json='{"base": {"first_name": "Ada"}, "extra": [{"field_id": "x", "value": "y"}]}',
            tag_ids="tag-1,tag-2",
        )
    )

    assert result["contact_id"] == "contact-1"
    assert captured[0]["method"] == "POST"
    assert captured[0]["url"] == "https://api.egoiapp.com/lists/list-1/contacts"
    assert captured[0]["headers"]["Apikey"] == "egoi-key"
    assert captured[0]["json_body"]["base"]["email"] == "ada@example.com"
    assert captured[0]["json_body"]["base"]["first_name"] == "Ada"
    assert captured[0]["json_body"]["extra"] == [{"field_id": "x", "value": "y"}]
    assert captured[1]["json_body"] == {"tag_id": "tag-1", "contacts": ["contact-1"]}
    assert captured[2]["json_body"] == {"tag_id": "tag-2", "contacts": ["contact-1"]}


def test_vero_track_event_uses_form_auth(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setenv("VERO_AUTH_TOKEN", "vero-token")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {"status": "ok"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.vero_track_event.func(
            user_id="user-1",
            email="ada@example.com",
            event_name="signed_up",
            data_json='{"plan": "pro"}',
            extras_json='{"source": "cli"}',
        )
    )

    assert result["status"] == "ok"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.getvero.com/api/v2/events/track"
    assert captured["data"]["auth_token"] == "vero-token"
    assert captured["data"]["event_name"] == "signed_up"
    assert json.loads(captured["data"]["data"]) == {"plan": "pro"}
    assert json.loads(captured["data"]["extras"]) == {"source": "cli"}


def test_mautic_list_contacts_uses_env_basic_auth(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MAUTIC_BASE_URL", "https://mautic.example")
    monkeypatch.setenv("MAUTIC_USERNAME", "alice")
    monkeypatch.setenv("MAUTIC_PASSWORD", "secret")
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {
            "contacts": {
                "1": {
                    "id": 1,
                    "fields": {"all": {"email": "ada@example.com", "firstname": "Ada"}},
                }
            },
            "total": "1",
        }

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(tools.mautic_list_contacts.func(search="ada", limit=5))

    expected_auth = base64.b64encode(b"alice:secret").decode()
    assert result == [{"email": "ada@example.com", "firstname": "Ada"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://mautic.example/api/contacts"
    assert captured["params"]["search"] == "ada"
    assert captured["params"]["limit"] == 5
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"


def test_mautic_create_contact_uses_vault_bearer_token(tmp_path, monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Mautic",
        provider="mautic",
        kind="oauth_token",
        allowed_targets=["native_tool:mautic_create_contact"],
        secret_fields={"access_token": "mautic-token", "base_url": "https://mautic.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {
            "contact": {
                "id": 2,
                "fields": {"all": {"email": kwargs["json_body"]["email"], "company": "Example"}},
            }
        }

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mautic_create_contact.func(
            email="ada@example.com",
            first_name="Ada",
            fields_json='{"company": "Example"}',
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["email"] == "ada@example.com"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://mautic.example/api/contacts/new"
    assert captured["headers"]["Authorization"] == "Bearer mautic-token"
    assert captured["json_body"]["email"] == "ada@example.com"
    assert captured["json_body"]["firstname"] == "Ada"
    assert captured["json_body"]["company"] == "Example"


def test_new_marketing_missing_keys_return_setup_hints(monkeypatch):
    from nymeria.tools import marketing_contact_service_integrations as tools

    monkeypatch.setattr(tools, "_settings_value", lambda name: None)
    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)

    assert "CUSTOMERIO_APP_API_KEY" in tools.customerio_list_campaigns.func()
    assert "CUSTOMERIO_TRACKING_SITE_ID" in tools.customerio_track_event.func("cust-1", "signed_up")
    assert "ITERABLE_API_KEY" in tools.iterable_list_lists.func()
    assert "POSTHOG_API_KEY" in tools.posthog_capture_event.func("signed_up", "user-1")
    assert "SEGMENT_WRITE_KEY" in tools.segment_identify.func(user_id="user-1")
    assert "ACTIONNETWORK_API_KEY" in tools.actionnetwork_list_records.func("person")
    assert "AUTOPILOT_API_KEY" in tools.autopilot_list_contacts.func()
    assert "EGOI_API_KEY" in tools.egoi_list_lists.func()
    assert "VERO_AUTH_TOKEN" in tools.vero_identify_user.func(user_id="user-1")
    assert "MAUTIC_BASE_URL" in tools.mautic_get_contact.func(contact_id="1")


def test_marketing_contact_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = [
        "customerio_list_campaigns",
        "customerio_get_campaign",
        "iterable_list_lists",
        "iterable_get_user",
        "activecampaign_list_contacts",
        "activecampaign_get_contact",
        "activecampaign_list_lists",
        "activecampaign_list_tags",
        "convertkit_get_account",
        "convertkit_list_forms",
        "convertkit_list_tags",
        "convertkit_list_subscribers",
        "getresponse_list_campaigns",
        "getresponse_list_contacts",
        "getresponse_get_contact",
        "mailerlite_list_subscribers",
        "mailerlite_get_subscriber",
        "mailerlite_list_groups",
        "actionnetwork_list_records",
        "actionnetwork_get_record",
        "autopilot_list_contacts",
        "autopilot_get_contact",
        "autopilot_list_lists",
        "egoi_list_lists",
        "egoi_list_contacts",
        "egoi_get_contact",
        "lemlist_list_campaigns",
        "lemlist_get_campaign_stats",
        "lemlist_list_activities",
        "lemlist_get_lead",
        "lemlist_get_team",
        "lemlist_get_team_credits",
        "lemlist_list_unsubscribes",
        "sendy_get_subscriber_status",
        "sendy_count_active_subscribers",
        "emelia_list_campaigns",
        "emelia_get_campaign",
        "emelia_list_contact_lists",
        "mautic_list_contacts",
        "mautic_get_contact",
        "mautic_list_companies",
        "mautic_get_company",
    ]
    moderate_names = [
        "activecampaign_sync_contact",
        "activecampaign_update_contact",
        "activecampaign_add_contact_to_list",
        "activecampaign_add_contact_tag",
        "convertkit_add_subscriber_to_form",
        "convertkit_add_subscriber_to_tag",
        "getresponse_create_contact",
        "getresponse_update_contact",
        "getresponse_delete_contact",
        "mailerlite_create_subscriber",
        "mailerlite_update_subscriber",
        "customerio_upsert_customer",
        "customerio_track_event",
        "customerio_track_anonymous_event",
        "customerio_update_segment",
        "iterable_upsert_user",
        "iterable_track_event",
        "iterable_update_list_subscribers",
        "posthog_capture_event",
        "posthog_identify",
        "posthog_create_alias",
        "posthog_track_page_or_screen",
        "segment_identify",
        "segment_track",
        "segment_group",
        "actionnetwork_create_person",
        "actionnetwork_update_person",
        "actionnetwork_create_event",
        "actionnetwork_create_petition",
        "actionnetwork_create_attendance",
        "actionnetwork_create_signature",
        "actionnetwork_add_person_tag",
        "actionnetwork_remove_person_tag",
        "autopilot_upsert_contact",
        "autopilot_delete_contact",
        "autopilot_create_list",
        "autopilot_update_contact_list_membership",
        "autopilot_add_contact_to_journey",
        "egoi_create_contact",
        "egoi_update_contact",
        "vero_identify_user",
        "vero_alias_user",
        "vero_update_user_subscription",
        "vero_update_user_tags",
        "vero_track_event",
        "lemlist_create_lead",
        "lemlist_remove_lead",
        "lemlist_update_unsubscribe",
        "sendy_create_campaign",
        "sendy_add_subscriber",
        "sendy_update_subscriber_subscription",
        "emelia_create_campaign",
        "emelia_update_campaign_status",
        "emelia_duplicate_campaign",
        "emelia_add_contact_to_campaign",
        "emelia_add_contact_to_list",
        "mautic_create_contact",
        "mautic_update_contact",
        "mautic_delete_contact",
        "mautic_create_company",
        "mautic_update_company",
        "mautic_delete_company",
        "mautic_add_contact_to_segment",
        "mautic_remove_contact_from_segment",
        "mautic_add_contact_to_campaign",
        "mautic_remove_contact_from_campaign",
        "mautic_add_contact_to_company",
        "mautic_remove_contact_from_company",
        "mautic_send_email_to_contact",
        "mautic_send_segment_email",
    ]

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


def test_marketing_contact_tool_schemas_hide_runtime_config():
    from nymeria.tools.marketing_contact_service_integrations import (
        activecampaign_sync_contact,
        actionnetwork_create_person,
        autopilot_upsert_contact,
        convertkit_add_subscriber_to_form,
        customerio_track_event,
        egoi_create_contact,
        emelia_create_campaign,
        lemlist_create_lead,
        mautic_create_contact,
        mailerlite_create_subscriber,
        posthog_capture_event,
        segment_track,
        sendy_add_subscriber,
        vero_track_event,
    )

    assert "config" not in activecampaign_sync_contact.args
    assert "config" not in actionnetwork_create_person.args
    assert "config" not in autopilot_upsert_contact.args
    assert "config" not in convertkit_add_subscriber_to_form.args
    assert "config" not in customerio_track_event.args
    assert "config" not in egoi_create_contact.args
    assert "config" not in emelia_create_campaign.args
    assert "config" not in lemlist_create_lead.args
    assert "config" not in mautic_create_contact.args
    assert "config" not in mailerlite_create_subscriber.args
    assert "config" not in posthog_capture_event.args
    assert "config" not in segment_track.args
    assert "config" not in sendy_add_subscriber.args
    assert "config" not in vero_track_event.args
