import base64
import json


from _service_integration_helpers import (  # type: ignore[import-not-found]
    bind_vault_repo as _use_repo,
    make_vault_repo as _repo,
)


def test_twilio_send_message_uses_env_basic_auth_and_form(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twilio-token")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "form_data": form_data,
                "headers": headers,
            }
        )
        return {"sid": "SM123", "status": "queued"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.twilio_send_message.func(
            from_number="+15550000001",
            to_number="+15550000002",
            body="Hello",
            status_callback="https://example.com/status",
        )
    )

    expected_auth = base64.b64encode(b"AC123:twilio-token").decode()
    assert result["sid"] == "SM123"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["form_data"]["From"] == "+15550000001"
    assert captured["form_data"]["To"] == "+15550000002"
    assert captured["form_data"]["Body"] == "Hello"
    assert captured["form_data"]["StatusCallback"] == "https://example.com/status"


def test_twilio_list_messages_uses_vault_api_key_sid(tmp_path, monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Twilio",
        provider="twilio",
        kind="api_key",
        allowed_targets=["native_tool:twilio_list_messages"],
        secret_fields={
            "account_sid": "AC123",
            "api_key_sid": "SK123",
            "api_key_secret": "twilio-secret",
            "base_url": "https://twilio.example/2010-04-01",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"messages": [{"sid": "SM1"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.twilio_list_messages.func(
            to_number="+15550000002",
            page_size=5,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    expected_auth = base64.b64encode(b"SK123:twilio-secret").decode()
    assert result == [{"sid": "SM1"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://twilio.example/2010-04-01/Accounts/AC123/Messages.json"
    assert captured["params"]["To"] == "+15550000002"
    assert captured["params"]["PageSize"] == 5
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"


def test_sendgrid_send_email_uses_env_key(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SENDGRID_API_KEY", "sendgrid-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"status": "ok", "status_code": 202}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.sendgrid_send_email.func(
            from_email="sender@example.com",
            to_emails="alice@example.com,bob@example.com",
            subject="Hello",
            text="Plain text",
            html="<p>HTML</p>",
            from_name="Sender",
            dynamic_template_data_json='{"name":"Alice"}',
        )
    )

    assert result["status_code"] == 202
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.sendgrid.com/v3/mail/send"
    assert captured["headers"]["Authorization"] == "Bearer sendgrid-key"
    assert captured["json_body"]["from"] == {"email": "sender@example.com", "name": "Sender"}
    assert captured["json_body"]["personalizations"][0]["to"] == [
        {"email": "alice@example.com"},
        {"email": "bob@example.com"},
    ]
    assert captured["json_body"]["personalizations"][0]["dynamic_template_data"] == {"name": "Alice"}
    assert captured["json_body"]["content"] == [
        {"type": "text/plain", "value": "Plain text"},
        {"type": "text/html", "value": "<p>HTML</p>"},
    ]


def test_sendgrid_upsert_contacts_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="SendGrid",
        provider="sendgrid",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={
            "api_key": "sendgrid-key",
            "base_url": "https://sendgrid.example/v3",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"job_id": "job-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.sendgrid_upsert_contacts.func(
            contacts_json='[{"email":"alice@example.com","first_name":"Alice"}]',
            list_ids="list-1,list-2",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["job_id"] == "job-1"
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://sendgrid.example/v3/marketing/contacts"
    assert captured["headers"]["Authorization"] == "Bearer sendgrid-key"
    assert captured["json_body"]["contacts"] == [{"email": "alice@example.com", "first_name": "Alice"}]
    assert captured["json_body"]["list_ids"] == ["list-1", "list-2"]


def test_mailgun_send_email_uses_env_key_and_domain(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MAILGUN_API_KEY", "mailgun-key")
    monkeypatch.setenv("MAILGUN_DOMAIN", "mg.example.com")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "form_data": form_data, "headers": headers})
        return {"id": "<message-id>", "message": "Queued"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mailgun_send_email.func(
            from_email="Sender <sender@example.com>",
            to_emails="alice@example.com",
            subject="Hello",
            text="Plain text",
            cc="cc@example.com",
            extra_fields_json='{"o:tag":"welcome"}',
        )
    )

    expected_auth = base64.b64encode(b"api:mailgun-key").decode()
    assert result["message"] == "Queued"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.mailgun.net/v3/mg.example.com/messages"
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["form_data"]["from"] == "Sender <sender@example.com>"
    assert captured["form_data"]["to"] == "alice@example.com"
    assert captured["form_data"]["cc"] == "cc@example.com"
    assert captured["form_data"]["o:tag"] == "welcome"


def test_mailgun_list_events_uses_vault(tmp_path, monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Mailgun",
        provider="mailgun",
        kind="api_key",
        allowed_targets=["native_tool:mailgun_list_events"],
        secret_fields={
            "api_key": "mailgun-key",
            "domain": "mg.example.com",
            "base_url": "https://mailgun.example/v3",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params, "headers": headers})
        return {"items": [{"event": "delivered"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mailgun_list_events.func(
            event="delivered",
            ascending=True,
            limit=10,
            config={"configurable": {"user_id": "alice"}},
        )
    )

    expected_auth = base64.b64encode(b"api:mailgun-key").decode()
    assert result == [{"event": "delivered"}]
    assert captured["method"] == "GET"
    assert captured["url"] == "https://mailgun.example/v3/mg.example.com/events"
    assert captured["params"]["event"] == "delivered"
    assert captured["params"]["ascending"] == "yes"
    assert captured["params"]["limit"] == 10
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"


def test_brevo_send_email_uses_env_key_and_payload(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("BREVO_API_KEY", "brevo-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"messageId": "<msg>"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.brevo_send_email.func(
            from_email="sender@example.com",
            to_emails="alice@example.com,bob@example.com",
            subject="Hello",
            html="<p>Hello</p>",
            from_name="Sender",
            params_json='{"name":"Alice"}',
            tags="welcome,trial",
        )
    )

    assert result["messageId"] == "<msg>"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.brevo.com/v3/smtp/email"
    assert captured["headers"]["api-key"] == "brevo-key"
    assert captured["json_body"]["sender"] == {"email": "sender@example.com", "name": "Sender"}
    assert captured["json_body"]["to"] == [{"email": "alice@example.com"}, {"email": "bob@example.com"}]
    assert captured["json_body"]["params"] == {"name": "Alice"}
    assert captured["json_body"]["tags"] == ["welcome", "trial"]


def test_brevo_update_contact_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Brevo",
        provider="brevo",
        kind="api_key",
        allowed_targets=["native_tool:brevo_update_contact"],
        secret_fields={"api_key": "brevo-key", "base_url": "https://brevo.example/v3"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"status": "ok", "status_code": 204}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.brevo_update_contact.func(
            identifier="alice@example.com",
            attributes_json='{"FIRSTNAME":"Alice"}',
            list_ids="4,5",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["status"] == "ok"
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://brevo.example/v3/contacts/alice%40example.com"
    assert captured["headers"]["api-key"] == "brevo-key"
    assert captured["json_body"]["attributes"] == {"FIRSTNAME": "Alice"}
    assert captured["json_body"]["listIds"] == [4, 5]


def test_mailjet_send_email_uses_env_basic_auth(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MAILJET_API_KEY", "mailjet-key")
    monkeypatch.setenv("MAILJET_SECRET_KEY", "mailjet-secret")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"Messages": [{"Status": "success"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mailjet_send_email.func(
            from_email="sender@example.com",
            to_emails="alice@example.com",
            subject="Hello",
            text="Plain",
            template_id=123,
            variables_json='{"first":"Alice"}',
        )
    )

    expected_auth = base64.b64encode(b"mailjet-key:mailjet-secret").decode()
    assert result == [{"Status": "success"}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.mailjet.com/v3.1/send"
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    message = captured["json_body"]["Messages"][0]
    assert message["TemplateID"] == 123
    assert message["TemplateLanguage"] is True
    assert message["Variables"] == {"first": "Alice"}


def test_mailjet_send_sms_uses_vault_token(tmp_path, monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Mailjet SMS",
        provider="mailjet",
        kind="api_key",
        allowed_targets=["native_tool:mailjet_send_sms"],
        secret_fields={"sms_token": "mailjet-sms-token", "base_url": "https://mailjet.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"ID": "sms-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mailjet_send_sms.func(
            from_name="Sender",
            to_number="+15550000002",
            text="Hello",
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["ID"] == "sms-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://mailjet.example/v4/sms-send"
    assert captured["headers"]["Authorization"] == "Bearer mailjet-sms-token"
    assert captured["json_body"] == {"From": "Sender", "To": "+15550000002", "Text": "Hello"}


def test_mandrill_send_template_uses_env_key(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MANDRILL_API_KEY", "mandrill-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body})
        return [{"email": "alice@example.com", "status": "sent"}]

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mandrill_send_template.func(
            template_name="welcome",
            from_email="sender@example.com",
            to_emails="alice@example.com",
            subject="Welcome",
            merge_vars_json='[{"name":"FIRST","content":"Alice"}]',
        )
    )

    assert result[0]["status"] == "sent"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://mandrillapp.com/api/1.0/messages/send-template.json"
    assert captured["json_body"]["key"] == "mandrill-key"
    assert captured["json_body"]["template_name"] == "welcome"
    assert captured["json_body"]["message"]["global_merge_vars"] == [{"name": "FIRST", "content": "Alice"}]


def test_messagebird_get_balance_uses_vault_key(tmp_path, monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="MessageBird",
        provider="messagebird",
        kind="api_key",
        allowed_targets=["native_tool:*"],
        secret_fields={"access_key": "bird-key", "base_url": "https://bird.example"},
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "headers": headers})
        return {"amount": 10}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.messagebird_get_balance.func(config={"configurable": {"user_id": "alice"}})
    )

    assert result["amount"] == 10
    assert captured["method"] == "GET"
    assert captured["url"] == "https://bird.example/balance"
    assert captured["headers"]["Authorization"] == "AccessKey bird-key"


def test_mocean_send_sms_uses_env_key_secret(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MOCEAN_API_KEY", "mocean-key")
    monkeypatch.setenv("MOCEAN_API_SECRET", "mocean-secret")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "form_data": form_data})
        return {"messages": [{"status": "0"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.mocean_send_sms.func(
            from_number="Sender",
            to_number="+15550000002",
            text="Hello",
            delivery_report_url="https://example.com/dlr",
        )
    )

    assert result == [{"status": "0"}]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://rest.moceanapi.com/rest/2/sms"
    assert captured["form_data"]["mocean-api-key"] == "mocean-key"
    assert captured["form_data"]["mocean-api-secret"] == "mocean-secret"
    assert captured["form_data"]["mocean-dlr-mask"] == "1"


def test_msg91_send_sms_uses_env_auth_key(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("MSG91_AUTH_KEY", "msg91-key")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params})
        return {"status": "ok", "text": "request-id"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.msg91_send_sms.func(
            sender_id="NYMERA",
            to_numbers="+15550000002,+15550000003",
            message="Hello",
        )
    )

    assert result["text"] == "request-id"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.msg91.com/api/sendhttp.php"
    assert captured["params"]["authkey"] == "msg91-key"
    assert captured["params"]["mobiles"] == "+15550000002,+15550000003"


def test_plivo_send_message_uses_env_basic_auth(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("PLIVO_AUTH_ID", "MA123")
    monkeypatch.setenv("PLIVO_AUTH_TOKEN", "plivo-token")
    monkeypatch.setenv("PLIVO_BASE_URL", "https://plivo.example/v1")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "json_body": json_body, "headers": headers})
        return {"message_uuid": ["uuid-1"], "api_id": "api-1"}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.plivo_send_message.func(
            from_number="+15550000001",
            to_numbers="+15550000002,+15550000003",
            text="Hello",
            callback_url="https://example.com/plivo",
        )
    )

    expected_auth = base64.b64encode(b"MA123:plivo-token").decode()
    assert result["message_uuid"] == ["uuid-1"]
    assert captured["method"] == "POST"
    assert captured["url"] == "https://plivo.example/v1/Account/MA123/Message/"
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["json_body"]["src"] == "+15550000001"
    assert captured["json_body"]["dst"] == "+15550000002<+15550000003"
    assert captured["json_body"]["url"] == "https://example.com/plivo"


def test_vonage_get_balance_uses_vault_credentials(tmp_path, monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    repo = _repo(tmp_path, monkeypatch)
    _use_repo(monkeypatch, repo)
    repo.create_credential(
        owner_type="user",
        owner_user_id="alice",
        name="Vonage",
        provider="vonage",
        kind="api_key",
        allowed_targets=["native_tool:vonage_get_balance"],
        secret_fields={
            "api_key": "vonage-key",
            "api_secret": "vonage-secret",
            "base_url": "https://vonage.example",
        },
        created_by_user_id="alice",
    )
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "params": params})
        return {"value": 12.34, "autoReload": False}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.vonage_get_balance.func(
            config={"configurable": {"user_id": "alice"}},
        )
    )

    assert result["value"] == 12.34
    assert captured["method"] == "GET"
    assert captured["url"] == "https://vonage.example/account/get-balance"
    assert captured["params"] == {"api_key": "vonage-key", "api_secret": "vonage-secret"}


def test_seven_send_sms_uses_env_api_key(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setenv("SEVEN_API_KEY", "seven-key")
    monkeypatch.setenv("SEVEN_BASE_URL", "https://seven.example/api")
    captured = {}

    def fake_request(method, url, params=None, json_body=None, form_data=None, headers=None):
        captured.update({"method": method, "url": url, "form_data": form_data, "headers": headers})
        return {"success": "100", "messages": [{"id": "msg-1"}]}

    monkeypatch.setattr(tools, "_request_json", fake_request)

    result = json.loads(
        tools.seven_send_sms.func(
            to_numbers="+15550000002,+15550000003",
            text="Hello",
            from_name="Nymeria",
            flash=True,
            label="test",
        )
    )

    assert result["success"] == "100"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://seven.example/api/sms"
    assert captured["headers"]["X-Api-Key"] == "seven-key"
    assert captured["form_data"]["to"] == "+15550000002,+15550000003"
    assert captured["form_data"]["from"] == "Nymeria"
    assert captured["form_data"]["flash"] == "1"
    assert captured["form_data"]["json"] == "1"


def test_messaging_delivery_missing_credentials_return_setup_hints(monkeypatch):
    from nymeria.tools import messaging_delivery_service_integrations as tools

    values = {}
    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: values.get(name))

    twilio_account = tools.twilio_list_messages.func()
    values["twilio_account_sid"] = "AC123"
    twilio_token = tools.twilio_get_message.func("SM123")
    sendgrid = tools.sendgrid_list_lists.func()
    mailgun_domain = tools.mailgun_get_domain.func()
    values["mailgun_domain"] = "mg.example.com"
    mailgun_key = tools.mailgun_get_domain.func()
    brevo = tools.brevo_list_contacts.func()
    mailjet = tools.mailjet_list_contacts.func()
    mailjet_sms = tools.mailjet_send_sms.func("Sender", "+15550000002", "Hello")
    mandrill = tools.mandrill_send_email.func("sender@example.com", "alice@example.com", "Hi", text="Hello")
    messagebird = tools.messagebird_get_balance.func()
    mocean = tools.mocean_get_balance.func()
    msg91 = tools.msg91_send_sms.func("NYMERA", "+15550000002", "Hello")
    plivo = tools.plivo_get_account.func()
    vonage = tools.vonage_get_balance.func()
    seven = tools.seven_get_balance.func()

    assert "No Twilio account SID found" in twilio_account
    assert "TWILIO_ACCOUNT_SID" in twilio_account
    assert "No Twilio credential found" in twilio_token
    assert "TWILIO_AUTH_TOKEN" in twilio_token
    assert "native_tool:twilio_get_message" in twilio_token
    assert "No SendGrid credential found" in sendgrid
    assert "SENDGRID_API_KEY" in sendgrid
    assert "native_tool:sendgrid_list_lists" in sendgrid
    assert "No Mailgun domain found" in mailgun_domain
    assert "MAILGUN_DOMAIN" in mailgun_domain
    assert "No Mailgun credential found" in mailgun_key
    assert "MAILGUN_API_KEY" in mailgun_key
    assert "native_tool:mailgun_get_domain" in mailgun_key
    assert "No Brevo credential found" in brevo
    assert "BREVO_API_KEY" in brevo
    assert "native_tool:brevo_list_contacts" in brevo
    assert "No Mailjet credential found" in mailjet
    assert "MAILJET_API_KEY + MAILJET_SECRET_KEY" in mailjet
    assert "No Mailjet SMS credential found" in mailjet_sms
    assert "MAILJET_SMS_TOKEN" in mailjet_sms
    assert "No Mandrill credential found" in mandrill
    assert "MANDRILL_API_KEY" in mandrill
    assert "No MessageBird credential found" in messagebird
    assert "MESSAGEBIRD_ACCESS_KEY" in messagebird
    assert "No Mocean credential found" in mocean
    assert "MOCEAN_API_KEY + MOCEAN_API_SECRET" in mocean
    assert "No MSG91 credential found" in msg91
    assert "MSG91_AUTH_KEY" in msg91
    assert "No Plivo credential found" in plivo
    assert "PLIVO_AUTH_ID + PLIVO_AUTH_TOKEN" in plivo
    assert "No Vonage credential found" in vonage
    assert "VONAGE_API_KEY + VONAGE_API_SECRET" in vonage
    assert "No seven.io credential found" in seven
    assert "SEVEN_API_KEY" in seven


def test_messaging_delivery_service_tools_are_registered_with_metadata():
    from nymeria.tools import CATALOG_TOOLS
    from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_tool_metadata

    safe_names = {
        "twilio_list_messages",
        "twilio_get_message",
        "sendgrid_list_contacts",
        "sendgrid_get_contact",
        "sendgrid_list_lists",
        "mailgun_list_events",
        "mailgun_get_domain",
        "brevo_list_contacts",
        "brevo_get_contact",
        "brevo_list_senders",
        "mailjet_list_contacts",
        "mailjet_get_contact",
        "messagebird_get_balance",
        "mocean_get_balance",
        "plivo_get_account",
        "vonage_get_balance",
        "seven_get_balance",
    }
    moderate_names = {
        "twilio_send_message",
        "twilio_make_call",
        "sendgrid_send_email",
        "sendgrid_upsert_contacts",
        "mailgun_send_email",
        "brevo_send_email",
        "brevo_create_contact",
        "brevo_update_contact",
        "mailjet_send_email",
        "mailjet_send_sms",
        "mandrill_send_email",
        "mandrill_send_template",
        "messagebird_send_sms",
        "mocean_send_sms",
        "mocean_send_voice",
        "msg91_send_sms",
        "plivo_send_message",
        "vonage_send_sms",
        "seven_send_sms",
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


def test_messaging_delivery_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.messaging_delivery_service_integrations import (
        mailgun_send_email,
        mandrill_send_email,
        messagebird_send_sms,
        mocean_send_sms,
        plivo_send_message,
        sendgrid_send_email,
        seven_send_sms,
        twilio_send_message,
        vonage_send_sms,
    )

    assert "config" not in twilio_send_message.args_schema.model_json_schema()["properties"]
    assert "config" not in sendgrid_send_email.args_schema.model_json_schema()["properties"]
    assert "config" not in mailgun_send_email.args_schema.model_json_schema()["properties"]
    assert "config" not in mandrill_send_email.args_schema.model_json_schema()["properties"]
    assert "config" not in messagebird_send_sms.args_schema.model_json_schema()["properties"]
    assert "config" not in mocean_send_sms.args_schema.model_json_schema()["properties"]
    assert "config" not in plivo_send_message.args_schema.model_json_schema()["properties"]
    assert "config" not in vonage_send_sms.args_schema.model_json_schema()["properties"]
    assert "config" not in seven_send_sms.args_schema.model_json_schema()["properties"]
