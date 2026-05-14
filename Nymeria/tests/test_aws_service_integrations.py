import base64
import json


def test_lambda_invoke_parses_payload_and_request(monkeypatch):
    from nymeria.tools import aws_service_integrations as tools

    captured = {}

    class Payload:
        def read(self):
            return b'{"ok": true}'

    class Client:
        def invoke(self, **kwargs):
            captured.update(kwargs)
            return {
                "StatusCode": 200,
                "ExecutedVersion": "7",
                "Payload": Payload(),
                "LogResult": base64.b64encode(b"log tail").decode("ascii"),
            }

    monkeypatch.setattr(
        tools,
        "_aws_client",
        lambda service, tool_name, config, region_name="": (Client(), None),
    )

    result = json.loads(
        tools.aws_lambda_invoke.func(
            function_name="worker",
            payload_json='{"message": "hi"}',
            qualifier="prod",
            invocation_type="RequestResponse",
            log_type="Tail",
        )
    )

    assert captured["FunctionName"] == "worker"
    assert captured["Qualifier"] == "prod"
    assert captured["InvocationType"] == "RequestResponse"
    assert json.loads(captured["Payload"].decode("utf-8")) == {"message": "hi"}
    assert result["payload"] == {"ok": True}
    assert result["log"] == "log tail"


def test_sns_publish_converts_simple_message_attributes(monkeypatch):
    from nymeria.tools import aws_service_integrations as tools

    captured = {}

    class Client:
        def publish(self, **kwargs):
            captured.update(kwargs)
            return {"MessageId": "msg-1"}

    monkeypatch.setattr(
        tools,
        "_aws_client",
        lambda service, tool_name, config, region_name="": (Client(), None),
    )

    result = json.loads(
        tools.aws_sns_publish.func(
            topic_arn="arn:aws:sns:us-east-1:123:alerts",
            message="Deploy done",
            subject="deploy",
            message_attributes_json='{"env": "prod", "attempt": {"DataType": "Number", "StringValue": "2"}}',
        )
    )

    assert result["MessageId"] == "msg-1"
    assert captured["Subject"] == "deploy"
    assert captured["MessageAttributes"]["env"] == {
        "DataType": "String",
        "StringValue": "prod",
    }
    assert captured["MessageAttributes"]["attempt"]["DataType"] == "Number"


def test_ses_send_email_builds_destination_and_body(monkeypatch):
    from nymeria.tools import aws_service_integrations as tools

    captured = {}

    class Client:
        def send_email(self, **kwargs):
            captured.update(kwargs)
            return {"MessageId": "ses-1"}

    monkeypatch.setattr(
        tools,
        "_aws_client",
        lambda service, tool_name, config, region_name="": (Client(), None),
    )

    result = json.loads(
        tools.aws_ses_send_email.func(
            source="sender@example.com",
            to_addresses="ada@example.com, grace@example.com",
            subject="Report",
            text_body="plain",
            html_body="<b>html</b>",
            cc_addresses="ops@example.com",
            reply_to_addresses="reply@example.com",
        )
    )

    assert result["MessageId"] == "ses-1"
    assert captured["Source"] == "sender@example.com"
    assert captured["Destination"]["ToAddresses"] == [
        "ada@example.com",
        "grace@example.com",
    ]
    assert captured["Destination"]["CcAddresses"] == ["ops@example.com"]
    assert captured["Message"]["Body"]["Text"]["Data"] == "plain"
    assert captured["Message"]["Body"]["Html"]["Data"] == "<b>html</b>"
    assert captured["ReplyToAddresses"] == ["reply@example.com"]


def test_textract_analyze_expense_decodes_and_simplifies(monkeypatch):
    from nymeria.tools import aws_service_integrations as tools

    captured = {}

    class Client:
        def analyze_expense(self, **kwargs):
            captured.update(kwargs)
            return {
                "DocumentMetadata": {"Pages": 1},
                "ExpenseDocuments": [
                    {
                        "ExpenseIndex": 1,
                        "SummaryFields": [
                            {
                                "Type": {"Text": "TOTAL"},
                                "LabelDetection": {"Text": "Total"},
                                "ValueDetection": {
                                    "Text": "$12.34",
                                    "Confidence": 98.5,
                                },
                            }
                        ],
                        "LineItemGroups": [
                            {
                                "LineItems": [
                                    {
                                        "LineItemExpenseFields": [
                                            {
                                                "Type": {"Text": "ITEM"},
                                                "ValueDetection": {"Text": "Coffee"},
                                            }
                                        ]
                                    }
                                ]
                            }
                        ],
                    }
                ],
            }

    monkeypatch.setattr(
        tools,
        "_aws_client",
        lambda service, tool_name, config, region_name="": (Client(), None),
    )

    document = base64.b64encode(b"fake-pdf").decode("ascii")
    result = json.loads(tools.aws_textract_analyze_expense.func(document_base64=document))

    assert captured["Document"]["Bytes"] == b"fake-pdf"
    assert result["document_metadata"] == {"Pages": 1}
    assert result["expense_documents"][0]["summary_fields"][0]["value"] == "$12.34"
    assert result["expense_documents"][0]["line_items"][0]["ITEM"] == "Coffee"


def test_transcribe_start_job_shapes_language_output_and_settings(monkeypatch):
    from nymeria.tools import aws_service_integrations as tools

    captured = {}

    class Client:
        def start_transcription_job(self, **kwargs):
            captured.update(kwargs)
            return {"TranscriptionJob": {"TranscriptionJobName": "call-1"}}

    monkeypatch.setattr(
        tools,
        "_aws_client",
        lambda service, tool_name, config, region_name="": (Client(), None),
    )

    result = json.loads(
        tools.aws_transcribe_start_job.func(
            job_name="call-1",
            media_file_uri="s3://audio/call.mp3",
            detect_language=True,
            output_bucket="transcripts",
            output_key="calls/call-1.json",
            settings_json='{"ShowSpeakerLabels": true, "MaxSpeakerLabels": 2}',
        )
    )

    assert result["TranscriptionJob"]["TranscriptionJobName"] == "call-1"
    assert captured["IdentifyLanguage"] is True
    assert "LanguageCode" not in captured
    assert captured["Media"] == {"MediaFileUri": "s3://audio/call.mp3"}
    assert captured["OutputBucketName"] == "transcripts"
    assert captured["OutputKey"] == "calls/call-1.json"
    assert captured["Settings"]["ShowSpeakerLabels"] is True


def test_aws_client_returns_credential_hint_without_keys(monkeypatch):
    from nymeria.tools import aws_service_integrations as tools

    monkeypatch.setattr(tools, "_credential_value", lambda **kwargs: None)
    monkeypatch.setattr(tools, "_settings_value", lambda name: None)

    client, error = tools._aws_client("lambda", "aws_lambda_list_functions", None)

    assert client is None
    assert "No AWS credential found" in error
    assert "native_tool:aws_lambda_list_functions" in error
    assert "AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY" in error


def test_aws_service_registration_and_security_metadata():
    from nymeria.tools import OPTIONAL_TOOLS
    from nymeria.tools.metadata import SecurityLevel, get_tool_metadata

    safe_names = {
        "aws_lambda_list_functions",
        "aws_sns_list_topics",
        "aws_ses_list_identities",
        "aws_ses_list_templates",
        "aws_ses_get_template",
        "aws_transcribe_get_job",
        "aws_transcribe_list_jobs",
    }
    moderate_names = {
        "aws_lambda_invoke",
        "aws_sns_create_topic",
        "aws_sns_publish",
        "aws_sns_delete_topic",
        "aws_ses_send_email",
        "aws_ses_verify_email_identity",
        "aws_ses_create_template",
        "aws_ses_update_template",
        "aws_ses_delete_template",
        "aws_textract_analyze_expense",
        "aws_transcribe_start_job",
        "aws_transcribe_delete_job",
    }

    for name in safe_names | moderate_names:
        assert name in OPTIONAL_TOOLS

    for name in safe_names:
        assert get_tool_metadata(name).security_level == SecurityLevel.SAFE

    for name in moderate_names:
        assert get_tool_metadata(name).security_level == SecurityLevel.MODERATE


def test_aws_service_tool_schemas_hide_runtime_config():
    from nymeria.tools.aws_service_integrations import (
        aws_lambda_invoke,
        aws_ses_send_email,
        aws_sns_publish,
        aws_textract_analyze_expense,
        aws_transcribe_start_job,
    )

    for tool_obj in [
        aws_lambda_invoke,
        aws_sns_publish,
        aws_ses_send_email,
        aws_textract_analyze_expense,
        aws_transcribe_start_job,
    ]:
        assert "config" not in tool_obj.args_schema.model_json_schema()["properties"]
