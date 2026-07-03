"""AWS service integration tools beyond S3."""

from __future__ import annotations

import base64
import json
import logging
from typing import Annotated, Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .credential_registry import (
    CredentialFieldGroup,
    ProviderCredentialSpec,
    register_provider_spec,
)

logger = logging.getLogger(__name__)

_MAX_JSON_CHARS = 70_000
_MAX_DOCUMENT_BYTES = 8_000_000

# Provider credential spec: the single source of truth for AWS credential shape
# (see credential_registry). The local _credential_value / _setup_hint wrappers
# and their call sites source their arguments from this spec; field-name tuple
# ORDER is behaviorally significant and must not be reordered.
_AWS = register_provider_spec(
    ProviderCredentialSpec(
        provider="aws",
        aliases=("amazon_web_services", "s3", "aws_s3", "aws_api"),
        groups=(
            CredentialFieldGroup(
                role="access_key", names=("access_key_id", "accessKeyId", "aws_access_key_id")
            ),
            CredentialFieldGroup(
                role="secret_key",
                names=("secret_access_key", "secretAccessKey", "aws_secret_access_key"),
            ),
            CredentialFieldGroup(
                role="session_token",
                names=("session_token", "sessionToken", "aws_session_token"),
                required=False,
            ),
            CredentialFieldGroup(role="region", names=("region", "aws_region"), required=False),
            CredentialFieldGroup(
                role="endpoint_url",
                names=("endpoint_url", "endpointUrl", "endpoint", "base_url", "baseUrl"),
                required=False,
            ),
        ),
        hint_fields=("access_key_id", "secret_access_key"),
        env_var="AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY",
        display_name="AWS",
        services=("aws_lambda", "aws_ses", "aws_sns", "aws_textract", "aws_transcribe"),
    )
)


def _dump_json(data: Any, *, max_chars: int = _MAX_JSON_CHARS) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"


def _json_object(value: str, *, field_name: str) -> dict[str, Any]:
    if not value or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _settings_value(name: str) -> Any:
    from ..config import get_settings

    return getattr(get_settings(), name)


def _credential_value(
    *,
    field_names: tuple[str, ...],
    tool_name: str,
    config: Optional[RunnableConfig],
) -> Optional[str]:
    from .native_credentials import get_native_credential_value

    credential = get_native_credential_value(
        provider=_AWS.provider,
        provider_aliases=_AWS.aliases,
        field_names=field_names,
        tool_name=tool_name,
        config=config,
    )
    return credential.value if credential else None


def _setup_hint(tool_name: str) -> str:
    from .native_credentials import native_credential_setup_hint

    return native_credential_setup_hint(
        provider=_AWS.provider,
        field_names=_AWS.hint_fields,
        tool_name=tool_name,
        env_var=_AWS.env_var,
        display_name=_AWS.display_name,
    )


def _aws_client(
    service_name: str,
    tool_name: str,
    config: Optional[RunnableConfig],
    *,
    region_name: str = "",
) -> tuple[Any, str | None]:
    access_key = _credential_value(
        field_names=_AWS.group("access_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("s3_access_key_id")
    secret_key = _credential_value(
        field_names=_AWS.group("secret_key"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("s3_secret_access_key")
    session_token = _credential_value(
        field_names=_AWS.group("session_token"),
        tool_name=tool_name,
        config=config,
    ) or _settings_value("s3_session_token")
    region = (
        region_name.strip()
        or _credential_value(field_names=_AWS.group("region"), tool_name=tool_name, config=config)
        or _settings_value("s3_region")
        or "us-east-1"
    )
    endpoint_url = _credential_value(
        field_names=_AWS.group("endpoint_url"),
        tool_name=tool_name,
        config=config,
    )
    if not access_key or not secret_key:
        return None, _setup_hint(tool_name)

    import boto3

    client_kwargs: dict[str, Any] = {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "region_name": region,
    }
    if session_token:
        client_kwargs["aws_session_token"] = session_token
    if endpoint_url:
        client_kwargs["endpoint_url"] = endpoint_url
    return boto3.client(service_name, **client_kwargs), None


def _payload_body(payload: Any) -> Any:
    if payload is None:
        return None
    if hasattr(payload, "read"):
        raw = payload.read()
        if isinstance(raw, bytes):
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return {"encoding": "base64", "data": base64.b64encode(raw).decode("ascii")}
        return raw
    return payload


def _message_attributes(value: str) -> dict[str, Any]:
    raw = _json_object(value, field_name="message_attributes_json")
    if not raw:
        return {}
    attrs: dict[str, Any] = {}
    for key, attr_value in raw.items():
        if isinstance(attr_value, dict) and "DataType" in attr_value:
            attrs[key] = attr_value
        else:
            attrs[key] = {"DataType": "String", "StringValue": str(attr_value)}
    return attrs


def _address_list(value: str) -> list[str]:
    return _split_csv(value)


def _ses_destination(
    to_addresses: str,
    cc_addresses: str,
    bcc_addresses: str,
) -> dict[str, list[str]]:
    destination = {"ToAddresses": _address_list(to_addresses)}
    if cc_addresses.strip():
        destination["CcAddresses"] = _address_list(cc_addresses)
    if bcc_addresses.strip():
        destination["BccAddresses"] = _address_list(bcc_addresses)
    return destination


def _simplify_expense_response(response: dict[str, Any]) -> dict[str, Any]:
    documents = []
    for doc in response.get("ExpenseDocuments", []) or []:
        summary_fields = []
        for field in doc.get("SummaryFields", []) or []:
            summary_fields.append(
                {
                    "type": (field.get("Type") or {}).get("Text"),
                    "label": (field.get("LabelDetection") or {}).get("Text"),
                    "value": (field.get("ValueDetection") or {}).get("Text"),
                    "confidence": (field.get("ValueDetection") or {}).get("Confidence"),
                    "currency": field.get("Currency"),
                }
            )
        line_items = []
        for group in doc.get("LineItemGroups", []) or []:
            for item in group.get("LineItems", []) or []:
                fields = {}
                for field in item.get("LineItemExpenseFields", []) or []:
                    field_type = (field.get("Type") or {}).get("Text") or "UNKNOWN"
                    fields[field_type] = (field.get("ValueDetection") or {}).get("Text")
                if fields:
                    line_items.append(fields)
        documents.append(
            {
                "expense_index": doc.get("ExpenseIndex"),
                "summary_fields": summary_fields,
                "line_items": line_items,
            }
        )
    return {"expense_documents": documents, "document_metadata": response.get("DocumentMetadata")}


@tool
def aws_lambda_list_functions(
    region_name: str = "",
    marker: str = "",
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List AWS Lambda functions.

    Args:
        region_name: Optional AWS region override.
        marker: Optional pagination marker.
        limit: Maximum functions to request, 1-50.
    """
    try:
        client, error = _aws_client("lambda", "aws_lambda_list_functions", config, region_name=region_name)
        if error:
            return error
        kwargs: dict[str, Any] = {"MaxItems": max(1, min(50, int(limit)))}
        if marker.strip():
            kwargs["Marker"] = marker.strip()
        return _dump_json(client.list_functions(**kwargs))
    except Exception as exc:
        logger.error("aws_lambda_list_functions failed", exc_info=True)
        return f"[Error]: AWS Lambda list failed: {exc}"


@tool
def aws_lambda_invoke(
    function_name: str,
    payload_json: str = "{}",
    qualifier: str = "",
    invocation_type: str = "RequestResponse",
    log_type: str = "None",
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Invoke an AWS Lambda function.

    Args:
        function_name: Function name or ARN.
        payload_json: JSON object or value to send as the Lambda payload.
        qualifier: Optional version or alias.
        invocation_type: RequestResponse, Event, or DryRun.
        log_type: None or Tail.
        region_name: Optional AWS region override.
    """
    if not function_name.strip():
        return "[Error]: function_name is required."
    try:
        payload = json.loads(payload_json or "{}")
        client, error = _aws_client("lambda", "aws_lambda_invoke", config, region_name=region_name)
        if error:
            return error
        kwargs: dict[str, Any] = {
            "FunctionName": function_name.strip(),
            "InvocationType": invocation_type.strip() or "RequestResponse",
            "LogType": log_type.strip() or "None",
            "Payload": json.dumps(payload).encode("utf-8"),
        }
        if qualifier.strip():
            kwargs["Qualifier"] = qualifier.strip()
        response = client.invoke(**kwargs)
        result = {key: value for key, value in response.items() if key != "Payload"}
        result["payload"] = _payload_body(response.get("Payload"))
        if result.get("LogResult"):
            try:
                result["log"] = base64.b64decode(str(result["LogResult"])).decode("utf-8", errors="replace")
            except Exception:
                logger.debug("AWS Lambda log tail decode failed", exc_info=True)
        return _dump_json(result)
    except Exception as exc:
        logger.error("aws_lambda_invoke failed", exc_info=True)
        return f"[Error]: AWS Lambda invoke failed: {exc}"


@tool
def aws_sns_list_topics(
    region_name: str = "",
    next_token: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List AWS SNS topics.

    Args:
        region_name: Optional AWS region override.
        next_token: Optional pagination token.
    """
    try:
        client, error = _aws_client("sns", "aws_sns_list_topics", config, region_name=region_name)
        if error:
            return error
        kwargs = {"NextToken": next_token.strip()} if next_token.strip() else {}
        return _dump_json(client.list_topics(**kwargs))
    except Exception as exc:
        logger.error("aws_sns_list_topics failed", exc_info=True)
        return f"[Error]: AWS SNS list topics failed: {exc}"


@tool
def aws_sns_create_topic(
    name: str,
    attributes_json: str = "",
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an AWS SNS topic.

    Args:
        name: Topic name.
        attributes_json: Optional SNS Attributes object.
        region_name: Optional AWS region override.
    """
    if not name.strip():
        return "[Error]: name is required."
    try:
        client, error = _aws_client("sns", "aws_sns_create_topic", config, region_name=region_name)
        if error:
            return error
        attrs = _json_object(attributes_json, field_name="attributes_json")
        kwargs: dict[str, Any] = {"Name": name.strip()}
        if attrs:
            kwargs["Attributes"] = {str(k): str(v) for k, v in attrs.items()}
        return _dump_json(client.create_topic(**kwargs))
    except Exception as exc:
        logger.error("aws_sns_create_topic failed", exc_info=True)
        return f"[Error]: AWS SNS create topic failed: {exc}"


@tool
def aws_sns_publish(
    topic_arn: str,
    message: str,
    subject: str = "",
    message_attributes_json: str = "",
    message_group_id: str = "",
    message_deduplication_id: str = "",
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Publish a message to an AWS SNS topic.

    Args:
        topic_arn: SNS topic ARN.
        message: Message body.
        subject: Optional email subject.
        message_attributes_json: Optional SNS MessageAttributes object or simple key-value object.
        message_group_id: FIFO message group ID.
        message_deduplication_id: FIFO deduplication ID.
        region_name: Optional AWS region override.
    """
    if not topic_arn.strip() or not message:
        return "[Error]: topic_arn and message are required."
    try:
        client, error = _aws_client("sns", "aws_sns_publish", config, region_name=region_name)
        if error:
            return error
        kwargs: dict[str, Any] = {"TopicArn": topic_arn.strip(), "Message": message}
        if subject.strip():
            kwargs["Subject"] = subject.strip()
        attrs = _message_attributes(message_attributes_json)
        if attrs:
            kwargs["MessageAttributes"] = attrs
        if message_group_id.strip():
            kwargs["MessageGroupId"] = message_group_id.strip()
        if message_deduplication_id.strip():
            kwargs["MessageDeduplicationId"] = message_deduplication_id.strip()
        return _dump_json(client.publish(**kwargs))
    except Exception as exc:
        logger.error("aws_sns_publish failed", exc_info=True)
        return f"[Error]: AWS SNS publish failed: {exc}"


@tool
def aws_sns_delete_topic(
    topic_arn: str,
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete an AWS SNS topic.

    Args:
        topic_arn: SNS topic ARN.
        region_name: Optional AWS region override.
    """
    if not topic_arn.strip():
        return "[Error]: topic_arn is required."
    try:
        client, error = _aws_client("sns", "aws_sns_delete_topic", config, region_name=region_name)
        if error:
            return error
        return _dump_json(client.delete_topic(TopicArn=topic_arn.strip()))
    except Exception as exc:
        logger.error("aws_sns_delete_topic failed", exc_info=True)
        return f"[Error]: AWS SNS delete topic failed: {exc}"


@tool
def aws_ses_send_email(
    source: str,
    to_addresses: str,
    subject: str,
    text_body: str = "",
    html_body: str = "",
    cc_addresses: str = "",
    bcc_addresses: str = "",
    reply_to_addresses: str = "",
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Send an email with Amazon SES.

    Args:
        source: Verified sender email address.
        to_addresses: Comma-separated recipient addresses.
        subject: Email subject.
        text_body: Plain text body.
        html_body: HTML body.
        cc_addresses: Comma-separated CC addresses.
        bcc_addresses: Comma-separated BCC addresses.
        reply_to_addresses: Comma-separated reply-to addresses.
        region_name: Optional AWS region override.
    """
    if not source.strip() or not to_addresses.strip() or not subject.strip():
        return "[Error]: source, to_addresses, and subject are required."
    if not text_body and not html_body:
        return "[Error]: text_body or html_body is required."
    try:
        client, error = _aws_client("ses", "aws_ses_send_email", config, region_name=region_name)
        if error:
            return error
        body: dict[str, Any] = {}
        if text_body:
            body["Text"] = {"Data": text_body, "Charset": "UTF-8"}
        if html_body:
            body["Html"] = {"Data": html_body, "Charset": "UTF-8"}
        kwargs: dict[str, Any] = {
            "Source": source.strip(),
            "Destination": _ses_destination(to_addresses, cc_addresses, bcc_addresses),
            "Message": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": body,
            },
        }
        replies = _address_list(reply_to_addresses)
        if replies:
            kwargs["ReplyToAddresses"] = replies
        return _dump_json(client.send_email(**kwargs))
    except Exception as exc:
        logger.error("aws_ses_send_email failed", exc_info=True)
        return f"[Error]: AWS SES send email failed: {exc}"


@tool
def aws_ses_list_identities(
    identity_type: str = "",
    next_token: str = "",
    limit: int = 100,
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Amazon SES identities.

    Args:
        identity_type: Optional EmailAddress or Domain.
        next_token: Optional pagination token.
        limit: Maximum identities to request, 1-1000.
        region_name: Optional AWS region override.
    """
    try:
        client, error = _aws_client("ses", "aws_ses_list_identities", config, region_name=region_name)
        if error:
            return error
        kwargs: dict[str, Any] = {"MaxItems": max(1, min(1000, int(limit)))}
        if identity_type.strip():
            kwargs["IdentityType"] = identity_type.strip()
        if next_token.strip():
            kwargs["NextToken"] = next_token.strip()
        return _dump_json(client.list_identities(**kwargs))
    except Exception as exc:
        logger.error("aws_ses_list_identities failed", exc_info=True)
        return f"[Error]: AWS SES list identities failed: {exc}"


@tool
def aws_ses_verify_email_identity(
    email_address: str,
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Start Amazon SES email identity verification.

    Args:
        email_address: Email address to verify.
        region_name: Optional AWS region override.
    """
    if not email_address.strip():
        return "[Error]: email_address is required."
    try:
        client, error = _aws_client("ses", "aws_ses_verify_email_identity", config, region_name=region_name)
        if error:
            return error
        return _dump_json(client.verify_email_identity(EmailAddress=email_address.strip()))
    except Exception as exc:
        logger.error("aws_ses_verify_email_identity failed", exc_info=True)
        return f"[Error]: AWS SES verify email identity failed: {exc}"


@tool
def aws_ses_list_templates(
    next_token: str = "",
    limit: int = 100,
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Amazon SES email templates.

    Args:
        next_token: Optional pagination token.
        limit: Maximum templates to request, 1-100.
        region_name: Optional AWS region override.
    """
    try:
        client, error = _aws_client("ses", "aws_ses_list_templates", config, region_name=region_name)
        if error:
            return error
        kwargs: dict[str, Any] = {"MaxItems": max(1, min(100, int(limit)))}
        if next_token.strip():
            kwargs["NextToken"] = next_token.strip()
        return _dump_json(client.list_templates(**kwargs))
    except Exception as exc:
        logger.error("aws_ses_list_templates failed", exc_info=True)
        return f"[Error]: AWS SES list templates failed: {exc}"


@tool
def aws_ses_get_template(
    template_name: str,
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Amazon SES email template.

    Args:
        template_name: Template name.
        region_name: Optional AWS region override.
    """
    if not template_name.strip():
        return "[Error]: template_name is required."
    try:
        client, error = _aws_client("ses", "aws_ses_get_template", config, region_name=region_name)
        if error:
            return error
        return _dump_json(client.get_template(TemplateName=template_name.strip()))
    except Exception as exc:
        logger.error("aws_ses_get_template failed", exc_info=True)
        return f"[Error]: AWS SES get template failed: {exc}"


def _template_payload(
    template_name: str,
    subject_part: str,
    text_part: str,
    html_part: str,
) -> dict[str, Any]:
    if not template_name.strip() or not subject_part.strip():
        raise ValueError("template_name and subject_part are required")
    template = {"TemplateName": template_name.strip(), "SubjectPart": subject_part}
    if text_part:
        template["TextPart"] = text_part
    if html_part:
        template["HtmlPart"] = html_part
    return {"Template": template}


@tool
def aws_ses_create_template(
    template_name: str,
    subject_part: str,
    text_part: str = "",
    html_part: str = "",
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Create an Amazon SES email template.

    Args:
        template_name: Template name.
        subject_part: Template subject.
        text_part: Plain text template body.
        html_part: HTML template body.
        region_name: Optional AWS region override.
    """
    try:
        client, error = _aws_client("ses", "aws_ses_create_template", config, region_name=region_name)
        if error:
            return error
        return _dump_json(client.create_template(**_template_payload(template_name, subject_part, text_part, html_part)))
    except Exception as exc:
        logger.error("aws_ses_create_template failed", exc_info=True)
        return f"[Error]: AWS SES create template failed: {exc}"


@tool
def aws_ses_update_template(
    template_name: str,
    subject_part: str,
    text_part: str = "",
    html_part: str = "",
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Update an Amazon SES email template.

    Args:
        template_name: Template name.
        subject_part: Template subject.
        text_part: Plain text template body.
        html_part: HTML template body.
        region_name: Optional AWS region override.
    """
    try:
        client, error = _aws_client("ses", "aws_ses_update_template", config, region_name=region_name)
        if error:
            return error
        return _dump_json(client.update_template(**_template_payload(template_name, subject_part, text_part, html_part)))
    except Exception as exc:
        logger.error("aws_ses_update_template failed", exc_info=True)
        return f"[Error]: AWS SES update template failed: {exc}"


@tool
def aws_ses_delete_template(
    template_name: str,
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete an Amazon SES email template.

    Args:
        template_name: Template name.
        region_name: Optional AWS region override.
    """
    if not template_name.strip():
        return "[Error]: template_name is required."
    try:
        client, error = _aws_client("ses", "aws_ses_delete_template", config, region_name=region_name)
        if error:
            return error
        return _dump_json(client.delete_template(TemplateName=template_name.strip()))
    except Exception as exc:
        logger.error("aws_ses_delete_template failed", exc_info=True)
        return f"[Error]: AWS SES delete template failed: {exc}"


@tool
def aws_textract_analyze_expense(
    document_base64: str,
    simplify: bool = True,
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Analyze a receipt or invoice image/PDF with Amazon Textract expense analysis.

    Args:
        document_base64: Base64-encoded document bytes.
        simplify: Whether to return a compact summary instead of raw Textract JSON.
        region_name: Optional AWS region override.
    """
    if not document_base64.strip():
        return "[Error]: document_base64 is required."
    try:
        document = base64.b64decode(document_base64, validate=True)
        if len(document) > _MAX_DOCUMENT_BYTES:
            return f"[Error]: document is too large; max {_MAX_DOCUMENT_BYTES} bytes."
        client, error = _aws_client("textract", "aws_textract_analyze_expense", config, region_name=region_name)
        if error:
            return error
        response = client.analyze_expense(Document={"Bytes": document})
        return _dump_json(_simplify_expense_response(response) if simplify else response)
    except Exception as exc:
        logger.error("aws_textract_analyze_expense failed", exc_info=True)
        return f"[Error]: AWS Textract analyze expense failed: {exc}"


@tool
def aws_transcribe_start_job(
    job_name: str,
    media_file_uri: str,
    language_code: str = "en-US",
    detect_language: bool = False,
    output_bucket: str = "",
    output_key: str = "",
    settings_json: str = "",
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Start an Amazon Transcribe transcription job.

    Args:
        job_name: Transcription job name.
        media_file_uri: S3 URI of the input media file.
        language_code: Language code used when detect_language is false.
        detect_language: Whether to enable automatic language identification.
        output_bucket: Optional output S3 bucket name.
        output_key: Optional output object key prefix.
        settings_json: Optional Transcribe Settings object.
        region_name: Optional AWS region override.
    """
    if not job_name.strip() or not media_file_uri.strip():
        return "[Error]: job_name and media_file_uri are required."
    try:
        client, error = _aws_client("transcribe", "aws_transcribe_start_job", config, region_name=region_name)
        if error:
            return error
        kwargs: dict[str, Any] = {
            "TranscriptionJobName": job_name.strip(),
            "Media": {"MediaFileUri": media_file_uri.strip()},
        }
        if detect_language:
            kwargs["IdentifyLanguage"] = True
        else:
            kwargs["LanguageCode"] = language_code.strip() or "en-US"
        if output_bucket.strip():
            kwargs["OutputBucketName"] = output_bucket.strip()
        if output_key.strip():
            kwargs["OutputKey"] = output_key.strip()
        settings = _json_object(settings_json, field_name="settings_json")
        if settings:
            kwargs["Settings"] = settings
        return _dump_json(client.start_transcription_job(**kwargs))
    except Exception as exc:
        logger.error("aws_transcribe_start_job failed", exc_info=True)
        return f"[Error]: AWS Transcribe start job failed: {exc}"


@tool
def aws_transcribe_get_job(
    job_name: str,
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Get an Amazon Transcribe job.

    Args:
        job_name: Transcription job name.
        region_name: Optional AWS region override.
    """
    if not job_name.strip():
        return "[Error]: job_name is required."
    try:
        client, error = _aws_client("transcribe", "aws_transcribe_get_job", config, region_name=region_name)
        if error:
            return error
        return _dump_json(client.get_transcription_job(TranscriptionJobName=job_name.strip()))
    except Exception as exc:
        logger.error("aws_transcribe_get_job failed", exc_info=True)
        return f"[Error]: AWS Transcribe get job failed: {exc}"


@tool
def aws_transcribe_list_jobs(
    status: str = "",
    job_name_contains: str = "",
    next_token: str = "",
    limit: int = 20,
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List Amazon Transcribe jobs.

    Args:
        status: Optional status filter, e.g. COMPLETED or IN_PROGRESS.
        job_name_contains: Optional name substring filter.
        next_token: Optional pagination token.
        limit: Maximum jobs to request, 1-100.
        region_name: Optional AWS region override.
    """
    try:
        client, error = _aws_client("transcribe", "aws_transcribe_list_jobs", config, region_name=region_name)
        if error:
            return error
        kwargs: dict[str, Any] = {"MaxResults": max(1, min(100, int(limit)))}
        if status.strip():
            kwargs["Status"] = status.strip()
        if job_name_contains.strip():
            kwargs["JobNameContains"] = job_name_contains.strip()
        if next_token.strip():
            kwargs["NextToken"] = next_token.strip()
        return _dump_json(client.list_transcription_jobs(**kwargs))
    except Exception as exc:
        logger.error("aws_transcribe_list_jobs failed", exc_info=True)
        return f"[Error]: AWS Transcribe list jobs failed: {exc}"


@tool
def aws_transcribe_delete_job(
    job_name: str,
    region_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Delete an Amazon Transcribe job.

    Args:
        job_name: Transcription job name.
        region_name: Optional AWS region override.
    """
    if not job_name.strip():
        return "[Error]: job_name is required."
    try:
        client, error = _aws_client("transcribe", "aws_transcribe_delete_job", config, region_name=region_name)
        if error:
            return error
        return _dump_json(client.delete_transcription_job(TranscriptionJobName=job_name.strip()))
    except Exception as exc:
        logger.error("aws_transcribe_delete_job failed", exc_info=True)
        return f"[Error]: AWS Transcribe delete job failed: {exc}"


AWS_SERVICE_TOOLS = [
    aws_lambda_list_functions,
    aws_lambda_invoke,
    aws_sns_list_topics,
    aws_sns_create_topic,
    aws_sns_publish,
    aws_sns_delete_topic,
    aws_ses_send_email,
    aws_ses_list_identities,
    aws_ses_verify_email_identity,
    aws_ses_list_templates,
    aws_ses_get_template,
    aws_ses_create_template,
    aws_ses_update_template,
    aws_ses_delete_template,
    aws_textract_analyze_expense,
    aws_transcribe_start_job,
    aws_transcribe_get_job,
    aws_transcribe_list_jobs,
    aws_transcribe_delete_job,
]
