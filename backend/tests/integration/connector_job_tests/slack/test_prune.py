import os
from datetime import datetime
from datetime import timezone
from typing import Any

import requests

from danswer.connectors.models import InputType
from danswer.db.enums import AccessType
from danswer.search.enums import LLMEvaluationType
from danswer.search.enums import SearchType
from danswer.search.models import RetrievalDetails
from danswer.server.documents.models import DocumentSource
from ee.danswer.server.query_and_chat.models import DocumentSearchRequest
from tests.integration.common_utils.constants import API_SERVER_URL
from tests.integration.common_utils.managers.cc_pair import CCPairManager
from tests.integration.common_utils.managers.connector import ConnectorManager
from tests.integration.common_utils.managers.credential import CredentialManager
from tests.integration.common_utils.managers.llm_provider import LLMProviderManager
from tests.integration.common_utils.managers.user import UserManager
from tests.integration.common_utils.test_models import DATestCCPair
from tests.integration.common_utils.test_models import DATestConnector
from tests.integration.common_utils.test_models import DATestCredential
from tests.integration.common_utils.test_models import DATestUser
from tests.integration.common_utils.vespa import vespa_fixture
from tests.integration.connector_job_tests.slack.slack_api_utils import SlackManager


def test_slack_prune(
    reset: None,
    vespa_client: vespa_fixture,
    slack_test_setup: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    public_channel, private_channel = slack_test_setup

    # Creating an admin user (first user created is automatically an admin)
    admin_user: DATestUser = UserManager.create(
        email="admin@onyx-test.com",
    )

    # Creating a non-admin user
    test_user_1: DATestUser = UserManager.create(
        email="test_user_1@onyx-test.com",
    )

    slack_client = SlackManager.get_slack_client(os.environ["SLACK_BOT_TOKEN"])
    email_id_map = SlackManager.build_slack_user_email_id_map(slack_client)
    admin_user_id = email_id_map[admin_user.email]

    LLMProviderManager.create(user_performing_action=admin_user)

    before = datetime.now(timezone.utc)
    credential: DATestCredential = CredentialManager.create(
        source=DocumentSource.SLACK,
        credential_json={
            "slack_bot_token": os.environ["SLACK_BOT_TOKEN"],
        },
        user_performing_action=admin_user,
    )
    connector: DATestConnector = ConnectorManager.create(
        name="Slack",
        input_type=InputType.POLL,
        source=DocumentSource.SLACK,
        connector_specific_config={
            "workspace": "onyx-test-workspace",
            "channels": [public_channel["name"], private_channel["name"]],
        },
        is_public=True,
        groups=[],
        user_performing_action=admin_user,
    )
    cc_pair: DATestCCPair = CCPairManager.create(
        credential_id=credential.id,
        connector_id=connector.id,
        access_type=AccessType.SYNC,
        user_performing_action=admin_user,
    )
    CCPairManager.wait_for_indexing(
        cc_pair=cc_pair,
        after=before,
        user_performing_action=admin_user,
    )

    # ----------------------SETUP INITIAL SLACK STATE--------------------------
    # Add test_user_1 and admin_user to the private channel
    desired_channel_members = [admin_user, test_user_1]
    SlackManager.set_channel_members(
        slack_client=slack_client,
        admin_user_id=admin_user_id,
        channel=private_channel,
        user_ids=[email_id_map[user.email] for user in desired_channel_members],
    )

    public_message = "Steve's favorite number is 809752"
    private_message = "Sara's favorite number is 346794"
    message_to_delete = "Rebecca's favorite number is 753468"

    SlackManager.add_message_to_channel(
        slack_client=slack_client,
        channel=public_channel,
        message=public_message,
    )
    SlackManager.add_message_to_channel(
        slack_client=slack_client,
        channel=private_channel,
        message=private_message,
    )
    SlackManager.add_message_to_channel(
        slack_client=slack_client,
        channel=private_channel,
        message=message_to_delete,
    )

    # Run indexing
    before = datetime.now(timezone.utc)
    CCPairManager.run_once(cc_pair, admin_user)
    CCPairManager.wait_for_indexing(
        cc_pair=cc_pair,
        after=before,
        user_performing_action=admin_user,
    )

    # Run permission sync
    before = datetime.now(timezone.utc)
    CCPairManager.sync(
        cc_pair=cc_pair,
        user_performing_action=admin_user,
    )
    CCPairManager.wait_for_sync(
        cc_pair=cc_pair,
        after=before,
        user_performing_action=admin_user,
    )

    # ----------------------TEST THE SETUP--------------------------
    # Search as admin with access to both channels
    search_request = DocumentSearchRequest(
        message="favorite number",
        search_type=SearchType.KEYWORD,
        retrieval_options=RetrievalDetails(),
        evaluation_type=LLMEvaluationType.SKIP,
    )
    search_request_body = search_request.model_dump()
    result = requests.post(
        url=f"{API_SERVER_URL}/query/document-search",
        json=search_request_body,
        headers=admin_user.headers,
    )
    result.raise_for_status()
    found_docs = result.json()["top_documents"]
    danswer_doc_message_strings = [doc["content"] for doc in found_docs]
    print(
        "\ntop_documents content before deleting for admin: ",
        danswer_doc_message_strings,
    )

    # Ensure admin user can see all messages
    assert public_message in danswer_doc_message_strings
    assert private_message in danswer_doc_message_strings
    assert message_to_delete in danswer_doc_message_strings

    # Search as test_user_1 with access to both channels
    search_request = DocumentSearchRequest(
        message="favorite number",
        search_type=SearchType.KEYWORD,
        retrieval_options=RetrievalDetails(),
        evaluation_type=LLMEvaluationType.SKIP,
    )
    search_request_body = search_request.model_dump()
    result = requests.post(
        url=f"{API_SERVER_URL}/query/document-search",
        json=search_request_body,
        headers=test_user_1.headers,
    )
    result.raise_for_status()
    found_docs = result.json()["top_documents"]
    danswer_doc_message_strings = [doc["content"] for doc in found_docs]
    print(
        "\ntop_documents content before deleting for test_user_1: ",
        danswer_doc_message_strings,
    )

    # Ensure test_user_1 can see all messages
    assert public_message in danswer_doc_message_strings
    assert private_message in danswer_doc_message_strings
    assert message_to_delete in danswer_doc_message_strings

    # ----------------------MAKE THE CHANGES--------------------------
    # Delete messages
    print("\nDeleting message: ", message_to_delete)
    SlackManager.remove_message_from_channel(
        slack_client=slack_client,
        channel=private_channel,
        message=message_to_delete,
    )

    # Prune the cc_pair
    now = datetime.now(timezone.utc)
    CCPairManager.prune(cc_pair, user_performing_action=admin_user)
    CCPairManager.wait_for_prune(cc_pair, now, user_performing_action=admin_user)

    # ----------------------------VERIFY THE CHANGES---------------------------
    # Ensure admin user can't see deleted messages
    # Search as admin user with access to only the public channel
    search_request = DocumentSearchRequest(
        message="favorite number",
        search_type=SearchType.KEYWORD,
        retrieval_options=RetrievalDetails(),
        evaluation_type=LLMEvaluationType.SKIP,
    )
    search_request_body = search_request.model_dump()
    result = requests.post(
        url=f"{API_SERVER_URL}/query/document-search",
        json=search_request_body,
        headers=admin_user.headers,
    )
    result.raise_for_status()
    found_docs = result.json()["top_documents"]
    danswer_doc_message_strings = [doc["content"] for doc in found_docs]
    print(
        "\ntop_documents content after deleting for admin: ",
        danswer_doc_message_strings,
    )

    # Ensure admin can't see deleted messages
    assert public_message in danswer_doc_message_strings
    assert private_message in danswer_doc_message_strings
    assert message_to_delete not in danswer_doc_message_strings

    # Ensure test_user_1 can't see deleted messages
    # Search as test_user_1 with access to only the public channel
    search_request = DocumentSearchRequest(
        message="favorite number",
        search_type=SearchType.KEYWORD,
        retrieval_options=RetrievalDetails(),
        evaluation_type=LLMEvaluationType.SKIP,
    )
    search_request_body = search_request.model_dump()
    result = requests.post(
        url=f"{API_SERVER_URL}/query/document-search",
        json=search_request_body,
        headers=test_user_1.headers,
    )
    result.raise_for_status()
    found_docs = result.json()["top_documents"]
    danswer_doc_message_strings = [doc["content"] for doc in found_docs]
    print(
        "\ntop_documents content after prune for test_user_1: ",
        danswer_doc_message_strings,
    )

    # Ensure test_user_1 can't see deleted messages
    assert public_message in danswer_doc_message_strings
    assert private_message in danswer_doc_message_strings
    assert message_to_delete not in danswer_doc_message_strings
