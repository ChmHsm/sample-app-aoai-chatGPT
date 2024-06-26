import copy
import json
import os
import logging
import uuid
from dotenv import load_dotenv
import httpx
from azure.storage.blob import BlobServiceClient
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes.aio import SearchIndexerClient
from backend.settings import (
    app_settings,
    MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
)
from quart import (
    abort,
    Blueprint,
    Quart,
    jsonify,
    make_response,
    request,
    send_from_directory,
    render_template,
)

from openai import AsyncAzureOpenAI
from azure.identity.aio import (
    DefaultAzureCredential,
    get_bearer_token_provider
)
from backend.auth.auth_utils import get_authenticated_user_details
from backend.security.ms_defender_utils import get_msdefender_user_json
from backend.history.cosmosdbservice import CosmosConversationClient
from backend.settings import (
    app_settings,
    MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
)
from backend.utils import (
    format_as_ndjson,
    format_stream_response,
    generateFilterString,
    generateFilterStringForConversation, parse_multi_columns,
    format_non_streaming_response,
    convert_to_pf_format,
    format_pf_non_streaming_response,
)
from azure.search.documents.indexes.models import SearchIndexerDataContainer, SearchIndexerDataSourceConnection, SearchIndexer

bp = Blueprint("routes", __name__, static_folder="static", template_folder="static")

load_dotenv()
# Current minimum Azure OpenAI version supported
MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION = "2024-02-15-preview"


# UI configuration (optional)
UI_TITLE = os.environ.get("UI_TITLE") or "Contoso"
UI_LOGO = os.environ.get("UI_LOGO")
UI_CHAT_LOGO = os.environ.get("UI_CHAT_LOGO")
UI_CHAT_TITLE = os.environ.get("UI_CHAT_TITLE") or "Start chatting"
UI_CHAT_DESCRIPTION = (
    os.environ.get("UI_CHAT_DESCRIPTION")
    or "This chatbot is configured to answer your questions"
)
UI_FAVICON = os.environ.get("UI_FAVICON") or "/favicon.ico"
UI_SHOW_SHARE_BUTTON = os.environ.get("UI_SHOW_SHARE_BUTTON", "true").lower() == "true"

DOCUPLOAD_MAX_SIZE_MB = os.environ.get("DOCUPLOAD_MAX_SIZE_MB")

def create_app():
    app = Quart(__name__)
    app.register_blueprint(bp)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    if DOCUPLOAD_MAX_SIZE_MB:
        app.config['MAX_CONTENT_LENGTH'] = int(DOCUPLOAD_MAX_SIZE_MB) * 1024 * 1024
    return app


@bp.route("/")
async def index():
    return await render_template(
        "index.html",
        title=app_settings.ui.title,
        favicon=app_settings.ui.favicon
    )


@bp.route("/favicon.ico")
async def favicon():
    return await bp.send_static_file("favicon.ico")


@bp.route("/assets/<path:path>")
async def assets(path):
    return await send_from_directory("static/assets", path)


# Debug settings
DEBUG = os.environ.get("DEBUG", "false")
if DEBUG.lower() == "true":
    logging.basicConfig(level=logging.DEBUG)

USER_AGENT = "GitHubSampleWebApp/AsyncAzureOpenAI/1.0.0"

# On Your Data Settings
DATASOURCE_TYPE = os.environ.get("DATASOURCE_TYPE", "AzureCognitiveSearch")
SEARCH_TOP_K = os.environ.get("SEARCH_TOP_K", 5)
SEARCH_STRICTNESS = os.environ.get("SEARCH_STRICTNESS", 3)
SEARCH_ENABLE_IN_DOMAIN = os.environ.get("SEARCH_ENABLE_IN_DOMAIN", "true")

# ACS Integration Settings
AZURE_SEARCH_SERVICE = os.environ.get("AZURE_SEARCH_SERVICE")
AZURE_SEARCH_INDEX = os.environ.get("AZURE_SEARCH_INDEX")
AZURE_SEARCH_KEY = os.environ.get("AZURE_SEARCH_KEY", None)
AZURE_SEARCH_USE_SEMANTIC_SEARCH = os.environ.get(
    "AZURE_SEARCH_USE_SEMANTIC_SEARCH", "false"
)
AZURE_SEARCH_SEMANTIC_SEARCH_CONFIG = os.environ.get(
    "AZURE_SEARCH_SEMANTIC_SEARCH_CONFIG", "default"
)
AZURE_SEARCH_TOP_K = os.environ.get("AZURE_SEARCH_TOP_K", SEARCH_TOP_K)
AZURE_SEARCH_ENABLE_IN_DOMAIN = os.environ.get(
    "AZURE_SEARCH_ENABLE_IN_DOMAIN", SEARCH_ENABLE_IN_DOMAIN
)
AZURE_SEARCH_CONTENT_COLUMNS = os.environ.get("AZURE_SEARCH_CONTENT_COLUMNS")
AZURE_SEARCH_FILENAME_COLUMN = os.environ.get("AZURE_SEARCH_FILENAME_COLUMN")
AZURE_SEARCH_TITLE_COLUMN = os.environ.get("AZURE_SEARCH_TITLE_COLUMN")
AZURE_SEARCH_URL_COLUMN = os.environ.get("AZURE_SEARCH_URL_COLUMN")
AZURE_SEARCH_VECTOR_COLUMNS = os.environ.get("AZURE_SEARCH_VECTOR_COLUMNS")
AZURE_SEARCH_QUERY_TYPE = os.environ.get("AZURE_SEARCH_QUERY_TYPE")
AZURE_SEARCH_PERMITTED_GROUPS_COLUMN = os.environ.get(
    "AZURE_SEARCH_PERMITTED_GROUPS_COLUMN"
)
AZURE_SEARCH_STRICTNESS = os.environ.get("AZURE_SEARCH_STRICTNESS", SEARCH_STRICTNESS)

# AOAI Integration Settings
AZURE_OPENAI_RESOURCE = os.environ.get("AZURE_OPENAI_RESOURCE")
AZURE_OPENAI_MODEL = os.environ.get("AZURE_OPENAI_MODEL")
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY")
AZURE_OPENAI_TEMPERATURE = os.environ.get("AZURE_OPENAI_TEMPERATURE", 0)
AZURE_OPENAI_TOP_P = os.environ.get("AZURE_OPENAI_TOP_P", 1.0)
AZURE_OPENAI_MAX_TOKENS = os.environ.get("AZURE_OPENAI_MAX_TOKENS", 1000)
AZURE_OPENAI_STOP_SEQUENCE = os.environ.get("AZURE_OPENAI_STOP_SEQUENCE")
AZURE_OPENAI_SYSTEM_MESSAGE = os.environ.get(
    "AZURE_OPENAI_SYSTEM_MESSAGE",
    "You are an AI assistant that helps people find information.",
)
AZURE_OPENAI_PREVIEW_API_VERSION = os.environ.get(
    "AZURE_OPENAI_PREVIEW_API_VERSION",
    MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION,
)
AZURE_OPENAI_STREAM = os.environ.get("AZURE_OPENAI_STREAM", "true")
AZURE_OPENAI_MODEL_NAME = os.environ.get(
    "AZURE_OPENAI_MODEL_NAME", "gpt-35-turbo-16k"
)  # Name of the model, e.g. 'gpt-35-turbo-16k' or 'gpt-4'
AZURE_OPENAI_EMBEDDING_ENDPOINT = os.environ.get("AZURE_OPENAI_EMBEDDING_ENDPOINT")
AZURE_OPENAI_EMBEDDING_KEY = os.environ.get("AZURE_OPENAI_EMBEDDING_KEY")
AZURE_OPENAI_EMBEDDING_NAME = os.environ.get("AZURE_OPENAI_EMBEDDING_NAME", "")

# CosmosDB Mongo vcore vector db Settings
AZURE_COSMOSDB_MONGO_VCORE_CONNECTION_STRING = os.environ.get(
    "AZURE_COSMOSDB_MONGO_VCORE_CONNECTION_STRING"
)  # This has to be secure string
AZURE_COSMOSDB_MONGO_VCORE_DATABASE = os.environ.get(
    "AZURE_COSMOSDB_MONGO_VCORE_DATABASE"
)
AZURE_COSMOSDB_MONGO_VCORE_CONTAINER = os.environ.get(
    "AZURE_COSMOSDB_MONGO_VCORE_CONTAINER"
)
AZURE_COSMOSDB_MONGO_VCORE_INDEX = os.environ.get("AZURE_COSMOSDB_MONGO_VCORE_INDEX")
AZURE_COSMOSDB_MONGO_VCORE_TOP_K = os.environ.get(
    "AZURE_COSMOSDB_MONGO_VCORE_TOP_K", AZURE_SEARCH_TOP_K
)
AZURE_COSMOSDB_MONGO_VCORE_STRICTNESS = os.environ.get(
    "AZURE_COSMOSDB_MONGO_VCORE_STRICTNESS", AZURE_SEARCH_STRICTNESS
)
AZURE_COSMOSDB_MONGO_VCORE_ENABLE_IN_DOMAIN = os.environ.get(
    "AZURE_COSMOSDB_MONGO_VCORE_ENABLE_IN_DOMAIN", AZURE_SEARCH_ENABLE_IN_DOMAIN
)
AZURE_COSMOSDB_MONGO_VCORE_CONTENT_COLUMNS = os.environ.get(
    "AZURE_COSMOSDB_MONGO_VCORE_CONTENT_COLUMNS", ""
)
AZURE_COSMOSDB_MONGO_VCORE_FILENAME_COLUMN = os.environ.get(
    "AZURE_COSMOSDB_MONGO_VCORE_FILENAME_COLUMN"
)
AZURE_COSMOSDB_MONGO_VCORE_TITLE_COLUMN = os.environ.get(
    "AZURE_COSMOSDB_MONGO_VCORE_TITLE_COLUMN"
)
AZURE_COSMOSDB_MONGO_VCORE_URL_COLUMN = os.environ.get(
    "AZURE_COSMOSDB_MONGO_VCORE_URL_COLUMN"
)
AZURE_COSMOSDB_MONGO_VCORE_VECTOR_COLUMNS = os.environ.get(
    "AZURE_COSMOSDB_MONGO_VCORE_VECTOR_COLUMNS"
)

SHOULD_STREAM = True if AZURE_OPENAI_STREAM.lower() == "true" else False

# Chat History CosmosDB Integration Settings
AZURE_COSMOSDB_DATABASE = os.environ.get("AZURE_COSMOSDB_DATABASE")
AZURE_COSMOSDB_ACCOUNT = os.environ.get("AZURE_COSMOSDB_ACCOUNT")
AZURE_COSMOSDB_CONVERSATIONS_CONTAINER = os.environ.get(
    "AZURE_COSMOSDB_CONVERSATIONS_CONTAINER"
)
AZURE_COSMOSDB_ACCOUNT_KEY = os.environ.get("AZURE_COSMOSDB_ACCOUNT_KEY")
AZURE_COSMOSDB_ENABLE_FEEDBACK = (
    os.environ.get("AZURE_COSMOSDB_ENABLE_FEEDBACK", "false").lower() == "true"
)

# Elasticsearch Integration Settings
ELASTICSEARCH_ENDPOINT = os.environ.get("ELASTICSEARCH_ENDPOINT")
ELASTICSEARCH_ENCODED_API_KEY = os.environ.get("ELASTICSEARCH_ENCODED_API_KEY")
ELASTICSEARCH_INDEX = os.environ.get("ELASTICSEARCH_INDEX")
ELASTICSEARCH_QUERY_TYPE = os.environ.get("ELASTICSEARCH_QUERY_TYPE", "simple")
ELASTICSEARCH_TOP_K = os.environ.get("ELASTICSEARCH_TOP_K", SEARCH_TOP_K)
ELASTICSEARCH_ENABLE_IN_DOMAIN = os.environ.get(
    "ELASTICSEARCH_ENABLE_IN_DOMAIN", SEARCH_ENABLE_IN_DOMAIN
)
ELASTICSEARCH_CONTENT_COLUMNS = os.environ.get("ELASTICSEARCH_CONTENT_COLUMNS")
ELASTICSEARCH_FILENAME_COLUMN = os.environ.get("ELASTICSEARCH_FILENAME_COLUMN")
ELASTICSEARCH_TITLE_COLUMN = os.environ.get("ELASTICSEARCH_TITLE_COLUMN")
ELASTICSEARCH_URL_COLUMN = os.environ.get("ELASTICSEARCH_URL_COLUMN")
ELASTICSEARCH_VECTOR_COLUMNS = os.environ.get("ELASTICSEARCH_VECTOR_COLUMNS")
ELASTICSEARCH_STRICTNESS = os.environ.get("ELASTICSEARCH_STRICTNESS", SEARCH_STRICTNESS)
ELASTICSEARCH_EMBEDDING_MODEL_ID = os.environ.get("ELASTICSEARCH_EMBEDDING_MODEL_ID")

# Pinecone Integration Settings
PINECONE_ENVIRONMENT = os.environ.get("PINECONE_ENVIRONMENT")
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
PINECONE_INDEX_NAME = os.environ.get("PINECONE_INDEX_NAME")
PINECONE_TOP_K = os.environ.get("PINECONE_TOP_K", SEARCH_TOP_K)
PINECONE_STRICTNESS = os.environ.get("PINECONE_STRICTNESS", SEARCH_STRICTNESS)
PINECONE_ENABLE_IN_DOMAIN = os.environ.get(
    "PINECONE_ENABLE_IN_DOMAIN", SEARCH_ENABLE_IN_DOMAIN
)
PINECONE_CONTENT_COLUMNS = os.environ.get("PINECONE_CONTENT_COLUMNS", "")
PINECONE_FILENAME_COLUMN = os.environ.get("PINECONE_FILENAME_COLUMN")
PINECONE_TITLE_COLUMN = os.environ.get("PINECONE_TITLE_COLUMN")
PINECONE_URL_COLUMN = os.environ.get("PINECONE_URL_COLUMN")
PINECONE_VECTOR_COLUMNS = os.environ.get("PINECONE_VECTOR_COLUMNS")

# Azure AI MLIndex Integration Settings - for use with MLIndex data assets created in Azure AI Studio
AZURE_MLINDEX_NAME = os.environ.get("AZURE_MLINDEX_NAME")
AZURE_MLINDEX_VERSION = os.environ.get("AZURE_MLINDEX_VERSION")
AZURE_ML_PROJECT_RESOURCE_ID = os.environ.get(
    "AZURE_ML_PROJECT_RESOURCE_ID"
)  # /subscriptions/{sub ID}/resourceGroups/{rg name}/providers/Microsoft.MachineLearningServices/workspaces/{AML project name}
AZURE_MLINDEX_TOP_K = os.environ.get("AZURE_MLINDEX_TOP_K", SEARCH_TOP_K)
AZURE_MLINDEX_STRICTNESS = os.environ.get("AZURE_MLINDEX_STRICTNESS", SEARCH_STRICTNESS)
AZURE_MLINDEX_ENABLE_IN_DOMAIN = os.environ.get(
    "AZURE_MLINDEX_ENABLE_IN_DOMAIN", SEARCH_ENABLE_IN_DOMAIN
)
AZURE_MLINDEX_CONTENT_COLUMNS = os.environ.get("AZURE_MLINDEX_CONTENT_COLUMNS", "")
AZURE_MLINDEX_FILENAME_COLUMN = os.environ.get("AZURE_MLINDEX_FILENAME_COLUMN")
AZURE_MLINDEX_TITLE_COLUMN = os.environ.get("AZURE_MLINDEX_TITLE_COLUMN")
AZURE_MLINDEX_URL_COLUMN = os.environ.get("AZURE_MLINDEX_URL_COLUMN")
AZURE_MLINDEX_VECTOR_COLUMNS = os.environ.get("AZURE_MLINDEX_VECTOR_COLUMNS")
AZURE_MLINDEX_QUERY_TYPE = os.environ.get("AZURE_MLINDEX_QUERY_TYPE")
# Promptflow Integration Settings
USE_PROMPTFLOW = os.environ.get("USE_PROMPTFLOW", "false").lower() == "true"
PROMPTFLOW_ENDPOINT = os.environ.get("PROMPTFLOW_ENDPOINT")
PROMPTFLOW_API_KEY = os.environ.get("PROMPTFLOW_API_KEY")
PROMPTFLOW_RESPONSE_TIMEOUT = os.environ.get("PROMPTFLOW_RESPONSE_TIMEOUT", 30.0)
# default request and response field names are input -> 'query' and output -> 'reply'
PROMPTFLOW_REQUEST_FIELD_NAME = os.environ.get("PROMPTFLOW_REQUEST_FIELD_NAME", "query")
PROMPTFLOW_RESPONSE_FIELD_NAME = os.environ.get(
    "PROMPTFLOW_RESPONSE_FIELD_NAME", "reply"
)
PROMPTFLOW_CITATIONS_FIELD_NAME = os.environ.get(
    "PROMPTFLOW_CITATIONS_FIELD_NAME", "documents"
)

# AZURE BLOB STORAGE
DOCUPLOAD_AZURE_BLOB_STORAGE_KEY = os.environ.get("DOCUPLOAD_AZURE_BLOB_STORAGE_KEY")
DOCUPLOAD_AZURE_BLOB_STORAGE_ACCOUNT_NAME = os.environ.get("DOCUPLOAD_AZURE_BLOB_STORAGE_ACCOUNT_NAME")
DOCUPLOAD_AZURE_BLOB_CONTAINER = os.environ.get("DOCUPLOAD_AZURE_BLOB_CONTAINER")
DOCUPLOAD_AZURE_BLOB_FOLDER = os.environ.get("DOCUPLOAD_AZURE_BLOB_FOLDER")
DOCUPLOAD_AZURE_SEARCH_INDEXER = os.environ.get("DOCUPLOAD_AZURE_SEARCH_INDEXER")
DOCUPLOAD_DELETE_BLOB_ON_CONVERSATION_DELETE = os.environ.get("DOCUPLOAD_DELETE_BLOB_ON_CONVERSATION_DELETE")
DOCUPLOAD_DELETE_INDEX_DOCUMENT_ON_CONVERSATION_DELETE = os.environ.get("DOCUPLOAD_DELETE_INDEX_DOCUMENT_ON_CONVERSATION_DELETE")
DOCUPLOAD_INDEX_DOCUMENT_KEY = os.environ.get("DOCUPLOAD_INDEX_DOCUMENT_KEY")
DOCUPLOAD_INDEX_POLLING_INTERVAL = os.environ.get("DOCUPLOAD_INDEX_POLLING_INTERVAL")

DOCUPLOAD_BLOB_CONNECTION_STRING = f"DefaultEndpointsProtocol=https;AccountName={DOCUPLOAD_AZURE_BLOB_STORAGE_ACCOUNT_NAME};AccountKey={DOCUPLOAD_AZURE_BLOB_STORAGE_KEY};EndpointSuffix=core.windows.net"

AZURE_SEARCH_ENDPOINT = f"https://{AZURE_SEARCH_SERVICE}.search.windows.net/"

# Frontend Settings via Environment Variables
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "true").lower() == "true"
CHAT_HISTORY_ENABLED = (
    AZURE_COSMOSDB_ACCOUNT
    and AZURE_COSMOSDB_DATABASE
    and AZURE_COSMOSDB_CONVERSATIONS_CONTAINER
)
SANITIZE_ANSWER = os.environ.get("SANITIZE_ANSWER", "false").lower() == "true"

def docupload_enabled():
    if AZURE_SEARCH_SERVICE and CHAT_HISTORY_ENABLED and DOCUPLOAD_AZURE_BLOB_STORAGE_KEY and DOCUPLOAD_AZURE_BLOB_STORAGE_ACCOUNT_NAME and DOCUPLOAD_AZURE_SEARCH_INDEXER:
        logging.debug("Doc Upload Feature Enabled")
        return True

DOCUPLOAD_ENABLED = docupload_enabled()


frontend_settings = {
    "auth_enabled": app_settings.base_settings.auth_enabled,
    "feedback_enabled": (
        app_settings.chat_history and
        app_settings.chat_history.enable_feedback
    ),
    "ui": {
        "title": UI_TITLE,
        "logo": UI_LOGO,
        "chat_logo": UI_CHAT_LOGO or UI_LOGO,
        "chat_title": UI_CHAT_TITLE,
        "chat_description": UI_CHAT_DESCRIPTION,
        "show_share_button": UI_SHOW_SHARE_BUTTON,
        "show_upload_button": DOCUPLOAD_ENABLED,
    },
    "sanitize_answer": SANITIZE_ANSWER,
    "polling_interval": DOCUPLOAD_INDEX_POLLING_INTERVAL,
    "upload_max_filesize": DOCUPLOAD_MAX_SIZE_MB,
}


# Enable Microsoft Defender for Cloud Integration
MS_DEFENDER_ENABLED = os.environ.get("MS_DEFENDER_ENABLED", "true").lower() == "true"

def should_use_data():
    global DATASOURCE_TYPE
    if AZURE_SEARCH_SERVICE and AZURE_SEARCH_INDEX:
        DATASOURCE_TYPE = "AzureCognitiveSearch"
        logging.debug("Using Azure Cognitive Search")
        return True

    if (
        AZURE_COSMOSDB_MONGO_VCORE_DATABASE
        and AZURE_COSMOSDB_MONGO_VCORE_CONTAINER
        and AZURE_COSMOSDB_MONGO_VCORE_INDEX
        and AZURE_COSMOSDB_MONGO_VCORE_CONNECTION_STRING
    ):
        DATASOURCE_TYPE = "AzureCosmosDB"
        logging.debug("Using Azure CosmosDB Mongo vcore")
        return True

    if ELASTICSEARCH_ENDPOINT and ELASTICSEARCH_ENCODED_API_KEY and ELASTICSEARCH_INDEX:
        DATASOURCE_TYPE = "Elasticsearch"
        logging.debug("Using Elasticsearch")
        return True

    if PINECONE_ENVIRONMENT and PINECONE_API_KEY and PINECONE_INDEX_NAME:
        DATASOURCE_TYPE = "Pinecone"
        logging.debug("Using Pinecone")
        return True

    if AZURE_MLINDEX_NAME and AZURE_MLINDEX_VERSION and AZURE_ML_PROJECT_RESOURCE_ID:
        DATASOURCE_TYPE = "AzureMLIndex"
        logging.debug("Using Azure ML Index")
        return True

    return False


SHOULD_USE_DATA = should_use_data()


# Initialize Azure OpenAI Client
def init_openai_client():
    azure_openai_client = None
    try:
        # API version check
        if (
            app_settings.azure_openai.preview_api_version
            < MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
        ):
            raise ValueError(
                f"The minimum supported Azure OpenAI preview API version is '{MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION}'"
            )

        # Endpoint
        if (
            not app_settings.azure_openai.endpoint and
            not app_settings.azure_openai.resource
        ):
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_RESOURCE is required"
            )

        endpoint = (
            app_settings.azure_openai.endpoint
            if app_settings.azure_openai.endpoint
            else f"https://{app_settings.azure_openai.resource}.openai.azure.com/"
        )

        # Authentication
        aoai_api_key = app_settings.azure_openai.key
        ad_token_provider = None
        if not aoai_api_key:
            logging.debug("No AZURE_OPENAI_KEY found, using Azure AD auth")
            ad_token_provider = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )

        # Deployment
        deployment = app_settings.azure_openai.model
        if not deployment:
            raise ValueError("AZURE_OPENAI_MODEL is required")

        # Default Headers
        default_headers = {"x-ms-useragent": USER_AGENT}

        azure_openai_client = AsyncAzureOpenAI(
            api_version=app_settings.azure_openai.preview_api_version,
            api_key=aoai_api_key,
            azure_ad_token_provider=ad_token_provider,
            default_headers=default_headers,
            azure_endpoint=endpoint,
        )

        return azure_openai_client
    except Exception as e:
        logging.exception("Exception in Azure OpenAI initialization", e)
        azure_openai_client = None
        raise e


def init_cosmosdb_client():
    cosmos_conversation_client = None
    if app_settings.chat_history:
        try:
            cosmos_endpoint = (
                f"https://{app_settings.chat_history.account}.documents.azure.com:443/"
            )

            if not app_settings.chat_history.account_key:
                credential = DefaultAzureCredential()
            else:
                credential = app_settings.chat_history.account_key

            cosmos_conversation_client = CosmosConversationClient(
                cosmosdb_endpoint=cosmos_endpoint,
                credential=credential,
                database_name=app_settings.chat_history.database,
                container_name=app_settings.chat_history.conversations_container,
                enable_message_feedback=app_settings.chat_history.enable_feedback,
            )
        except Exception as e:
            logging.exception("Exception in CosmosDB initialization", e)
            cosmos_conversation_client = None
            raise e
    else:
        logging.debug("CosmosDB not configured")

    return cosmos_conversation_client

def get_configured_data_source(conversation_id):
    data_source = {}
    query_type = "simple"
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user['user_principal_id']
    if DATASOURCE_TYPE == "AzureCognitiveSearch":
        # Set query type
        if AZURE_SEARCH_QUERY_TYPE:
            query_type = AZURE_SEARCH_QUERY_TYPE
        elif (
            AZURE_SEARCH_USE_SEMANTIC_SEARCH.lower() == "true"
            and AZURE_SEARCH_SEMANTIC_SEARCH_CONFIG
        ):
            query_type = "semantic"

        # Set filter
        filter = None
        userToken = None
        if AZURE_SEARCH_PERMITTED_GROUPS_COLUMN:
            userToken = request.headers.get("X-MS-TOKEN-AAD-ACCESS-TOKEN", "")
            logging.debug(f"USER TOKEN is {'present' if userToken else 'not present'}")
            if not userToken:
                raise Exception(
                    "Document-level access control is enabled, but user access token could not be fetched."
                )

            filter = generateFilterString(userToken)
            logging.debug(f"FILTER: {filter}")

        
        # Filter data by conversation
        filter = generateFilterStringForConversation(filter, user_id, conversation_id)

        # Set authentication
        authentication = {}
        if AZURE_SEARCH_KEY:
            authentication = {"type": "api_key", "api_key": AZURE_SEARCH_KEY}
        else:
            # If key is not provided, assume AOAI resource identity has been granted access to the search service
            authentication = {"type": "system_assigned_managed_identity"}

        data_source = {
            "type": "azure_search",
            "parameters": {
                "endpoint": AZURE_SEARCH_ENDPOINT,
                "authentication": authentication,
                "index_name": AZURE_SEARCH_INDEX,
                "fields_mapping": {
                    "content_fields": (
                        parse_multi_columns(AZURE_SEARCH_CONTENT_COLUMNS)
                        if AZURE_SEARCH_CONTENT_COLUMNS
                        else []
                    ),
                    "title_field": (
                        AZURE_SEARCH_TITLE_COLUMN if AZURE_SEARCH_TITLE_COLUMN else None
                    ),
                    "url_field": (
                        AZURE_SEARCH_URL_COLUMN if AZURE_SEARCH_URL_COLUMN else None
                    ),
                    "filepath_field": (
                        AZURE_SEARCH_FILENAME_COLUMN
                        if AZURE_SEARCH_FILENAME_COLUMN
                        else None
                    ),
                    "vector_fields": (
                        parse_multi_columns(AZURE_SEARCH_VECTOR_COLUMNS)
                        if AZURE_SEARCH_VECTOR_COLUMNS
                        else []
                    ),
                },
                "in_scope": (
                    True if AZURE_SEARCH_ENABLE_IN_DOMAIN.lower() == "true" else False
                ),
                "top_n_documents": (
                    int(AZURE_SEARCH_TOP_K) if AZURE_SEARCH_TOP_K else int(SEARCH_TOP_K)
                ),
                "query_type": query_type,
                "semantic_configuration": (
                    AZURE_SEARCH_SEMANTIC_SEARCH_CONFIG
                    if AZURE_SEARCH_SEMANTIC_SEARCH_CONFIG
                    else ""
                ),
                "role_information": AZURE_OPENAI_SYSTEM_MESSAGE,
                "filter": filter,
                "strictness": (
                    int(AZURE_SEARCH_STRICTNESS)
                    if AZURE_SEARCH_STRICTNESS
                    else int(SEARCH_STRICTNESS)
                ),
            },
        }
    elif DATASOURCE_TYPE == "AzureCosmosDB":
        query_type = "vector"

        data_source = {
            "type": "azure_cosmos_db",
            "parameters": {
                "authentication": {
                    "type": "connection_string",
                    "connection_string": AZURE_COSMOSDB_MONGO_VCORE_CONNECTION_STRING,
                },
                "index_name": AZURE_COSMOSDB_MONGO_VCORE_INDEX,
                "database_name": AZURE_COSMOSDB_MONGO_VCORE_DATABASE,
                "container_name": AZURE_COSMOSDB_MONGO_VCORE_CONTAINER,
                "fields_mapping": {
                    "content_fields": (
                        parse_multi_columns(AZURE_COSMOSDB_MONGO_VCORE_CONTENT_COLUMNS)
                        if AZURE_COSMOSDB_MONGO_VCORE_CONTENT_COLUMNS
                        else []
                    ),
                    "title_field": (
                        AZURE_COSMOSDB_MONGO_VCORE_TITLE_COLUMN
                        if AZURE_COSMOSDB_MONGO_VCORE_TITLE_COLUMN
                        else None
                    ),
                    "url_field": (
                        AZURE_COSMOSDB_MONGO_VCORE_URL_COLUMN
                        if AZURE_COSMOSDB_MONGO_VCORE_URL_COLUMN
                        else None
                    ),
                    "filepath_field": (
                        AZURE_COSMOSDB_MONGO_VCORE_FILENAME_COLUMN
                        if AZURE_COSMOSDB_MONGO_VCORE_FILENAME_COLUMN
                        else None
                    ),
                    "vector_fields": (
                        parse_multi_columns(AZURE_COSMOSDB_MONGO_VCORE_VECTOR_COLUMNS)
                        if AZURE_COSMOSDB_MONGO_VCORE_VECTOR_COLUMNS
                        else []
                    ),
                },
                "in_scope": (
                    True
                    if AZURE_COSMOSDB_MONGO_VCORE_ENABLE_IN_DOMAIN.lower() == "true"
                    else False
                ),
                "top_n_documents": (
                    int(AZURE_COSMOSDB_MONGO_VCORE_TOP_K)
                    if AZURE_COSMOSDB_MONGO_VCORE_TOP_K
                    else int(SEARCH_TOP_K)
                ),
                "strictness": (
                    int(AZURE_COSMOSDB_MONGO_VCORE_STRICTNESS)
                    if AZURE_COSMOSDB_MONGO_VCORE_STRICTNESS
                    else int(SEARCH_STRICTNESS)
                ),
                "query_type": query_type,
                "role_information": AZURE_OPENAI_SYSTEM_MESSAGE,
            },
        }
    elif DATASOURCE_TYPE == "Elasticsearch":
        if ELASTICSEARCH_QUERY_TYPE:
            query_type = ELASTICSEARCH_QUERY_TYPE

        data_source = {
            "type": "elasticsearch",
            "parameters": {
                "endpoint": ELASTICSEARCH_ENDPOINT,
                "authentication": {
                    "type": "encoded_api_key",
                    "encoded_api_key": ELASTICSEARCH_ENCODED_API_KEY,
                },
                "index_name": ELASTICSEARCH_INDEX,
                "fields_mapping": {
                    "content_fields": (
                        parse_multi_columns(ELASTICSEARCH_CONTENT_COLUMNS)
                        if ELASTICSEARCH_CONTENT_COLUMNS
                        else []
                    ),
                    "title_field": (
                        ELASTICSEARCH_TITLE_COLUMN
                        if ELASTICSEARCH_TITLE_COLUMN
                        else None
                    ),
                    "url_field": (
                        ELASTICSEARCH_URL_COLUMN if ELASTICSEARCH_URL_COLUMN else None
                    ),
                    "filepath_field": (
                        ELASTICSEARCH_FILENAME_COLUMN
                        if ELASTICSEARCH_FILENAME_COLUMN
                        else None
                    ),
                    "vector_fields": (
                        parse_multi_columns(ELASTICSEARCH_VECTOR_COLUMNS)
                        if ELASTICSEARCH_VECTOR_COLUMNS
                        else []
                    ),
                },
                "in_scope": (
                    True if ELASTICSEARCH_ENABLE_IN_DOMAIN.lower() == "true" else False
                ),
                "top_n_documents": (
                    int(ELASTICSEARCH_TOP_K)
                    if ELASTICSEARCH_TOP_K
                    else int(SEARCH_TOP_K)
                ),
                "query_type": query_type,
                "role_information": AZURE_OPENAI_SYSTEM_MESSAGE,
                "strictness": (
                    int(ELASTICSEARCH_STRICTNESS)
                    if ELASTICSEARCH_STRICTNESS
                    else int(SEARCH_STRICTNESS)
                ),
            },
        }
    elif DATASOURCE_TYPE == "AzureMLIndex":
        if AZURE_MLINDEX_QUERY_TYPE:
            query_type = AZURE_MLINDEX_QUERY_TYPE

        data_source = {
            "type": "azure_ml_index",
            "parameters": {
                "name": AZURE_MLINDEX_NAME,
                "version": AZURE_MLINDEX_VERSION,
                "project_resource_id": AZURE_ML_PROJECT_RESOURCE_ID,
                "fieldsMapping": {
                    "content_fields": (
                        parse_multi_columns(AZURE_MLINDEX_CONTENT_COLUMNS)
                        if AZURE_MLINDEX_CONTENT_COLUMNS
                        else []
                    ),
                    "title_field": (
                        AZURE_MLINDEX_TITLE_COLUMN
                        if AZURE_MLINDEX_TITLE_COLUMN
                        else None
                    ),
                    "url_field": (
                        AZURE_MLINDEX_URL_COLUMN if AZURE_MLINDEX_URL_COLUMN else None
                    ),
                    "filepath_field": (
                        AZURE_MLINDEX_FILENAME_COLUMN
                        if AZURE_MLINDEX_FILENAME_COLUMN
                        else None
                    ),
                    "vector_fields": (
                        parse_multi_columns(AZURE_MLINDEX_VECTOR_COLUMNS)
                        if AZURE_MLINDEX_VECTOR_COLUMNS
                        else []
                    ),
                },
                "in_scope": (
                    True if AZURE_MLINDEX_ENABLE_IN_DOMAIN.lower() == "true" else False
                ),
                "top_n_documents": (
                    int(AZURE_MLINDEX_TOP_K)
                    if AZURE_MLINDEX_TOP_K
                    else int(SEARCH_TOP_K)
                ),
                "query_type": query_type,
                "role_information": AZURE_OPENAI_SYSTEM_MESSAGE,
                "strictness": (
                    int(AZURE_MLINDEX_STRICTNESS)
                    if AZURE_MLINDEX_STRICTNESS
                    else int(SEARCH_STRICTNESS)
                ),
            },
        }
    elif DATASOURCE_TYPE == "Pinecone":
        query_type = "vector"

        data_source = {
            "type": "pinecone",
            "parameters": {
                "environment": PINECONE_ENVIRONMENT,
                "authentication": {"type": "api_key", "key": PINECONE_API_KEY},
                "index_name": PINECONE_INDEX_NAME,
                "fields_mapping": {
                    "content_fields": (
                        parse_multi_columns(PINECONE_CONTENT_COLUMNS)
                        if PINECONE_CONTENT_COLUMNS
                        else []
                    ),
                    "title_field": (
                        PINECONE_TITLE_COLUMN if PINECONE_TITLE_COLUMN else None
                    ),
                    "url_field": PINECONE_URL_COLUMN if PINECONE_URL_COLUMN else None,
                    "filepath_field": (
                        PINECONE_FILENAME_COLUMN if PINECONE_FILENAME_COLUMN else None
                    ),
                    "vector_fields": (
                        parse_multi_columns(PINECONE_VECTOR_COLUMNS)
                        if PINECONE_VECTOR_COLUMNS
                        else []
                    ),
                },
                "in_scope": (
                    True if PINECONE_ENABLE_IN_DOMAIN.lower() == "true" else False
                ),
                "top_n_documents": (
                    int(PINECONE_TOP_K) if PINECONE_TOP_K else int(SEARCH_TOP_K)
                ),
                "strictness": (
                    int(PINECONE_STRICTNESS)
                    if PINECONE_STRICTNESS
                    else int(SEARCH_STRICTNESS)
                ),
                "query_type": query_type,
                "role_information": AZURE_OPENAI_SYSTEM_MESSAGE,
            },
        }
    else:
        raise Exception(
            f"DATASOURCE_TYPE is not configured or unknown: {DATASOURCE_TYPE}"
        )

    if "vector" in query_type.lower() and DATASOURCE_TYPE != "AzureMLIndex":
        embeddingDependency = {}
        if AZURE_OPENAI_EMBEDDING_NAME:
            embeddingDependency = {
                "type": "deployment_name",
                "deployment_name": AZURE_OPENAI_EMBEDDING_NAME,
            }
        elif AZURE_OPENAI_EMBEDDING_ENDPOINT and AZURE_OPENAI_EMBEDDING_KEY:
            embeddingDependency = {
                "type": "endpoint",
                "endpoint": AZURE_OPENAI_EMBEDDING_ENDPOINT,
                "authentication": {
                    "type": "api_key",
                    "key": AZURE_OPENAI_EMBEDDING_KEY,
                },
            }
        elif DATASOURCE_TYPE == "Elasticsearch" and ELASTICSEARCH_EMBEDDING_MODEL_ID:
            embeddingDependency = {
                "type": "model_id",
                "model_id": ELASTICSEARCH_EMBEDDING_MODEL_ID,
            }
        else:
            raise Exception(
                f"Vector query type ({query_type}) is selected for data source type {DATASOURCE_TYPE} but no embedding dependency is configured"
            )
        data_source["parameters"]["embedding_dependency"] = embeddingDependency

    return data_source


def prepare_model_args(request_body, request_headers):
    request_messages = request_body.get("messages", [])
    # conversation_id = request_body.get('conversation_id', None)
    # if conversation_id is None:
    #     conversation_id = request_body['history_metadata']['conversation_id']
    print(f"Messages array {request_messages}")

    messages = []
    if not SHOULD_USE_DATA:
        messages = [{"role": "system", "content": AZURE_OPENAI_SYSTEM_MESSAGE}]
    content_list = []
    for message in request_messages:
        if message and message["content"]:
            if message.get("type") and message.get("type") == "img":
                content_list.append({"type": "image_url", "image_url": {"url": message['content']} })
            else:
                content_list.append({"type": "text", "text": message['content']})
    messages.append({"role": message["role"], "content": content_list})

    user_json = None
    if (MS_DEFENDER_ENABLED):
        authenticated_user_details = get_authenticated_user_details(request_headers)
        user_json = get_msdefender_user_json(authenticated_user_details, request_headers)

    model_args = {
        "messages": messages,
        "temperature": app_settings.azure_openai.temperature,
        "max_tokens": app_settings.azure_openai.max_tokens,
        "top_p": app_settings.azure_openai.top_p,
        "stop": app_settings.azure_openai.stop_sequence,
        "stream": app_settings.azure_openai.stream,
        "model": app_settings.azure_openai.model,
        "user": user_json
    }

    if SHOULD_USE_DATA:
        model_args["extra_body"] = {"data_sources": [get_configured_data_source(conversation_id)]}

    model_args_clean = copy.deepcopy(model_args)
    if model_args_clean.get("extra_body"):
        secret_params = [
            "key",
            "connection_string",
            "embedding_key",
            "encoded_api_key",
            "api_key",
        ]
        for secret_param in secret_params:
            if model_args_clean["extra_body"]["data_sources"][0]["parameters"].get(
                secret_param
            ):
                model_args_clean["extra_body"]["data_sources"][0]["parameters"][
                    secret_param
                ] = "*****"
        authentication = model_args_clean["extra_body"]["data_sources"][0][
            "parameters"
        ].get("authentication", {})
        for field in authentication:
            if field in secret_params:
                model_args_clean["extra_body"]["data_sources"][0]["parameters"][
                    "authentication"
                ][field] = "*****"
        embeddingDependency = model_args_clean["extra_body"]["data_sources"][0][
            "parameters"
        ].get("embedding_dependency", {})
        if "authentication" in embeddingDependency:
            for field in embeddingDependency["authentication"]:
                if field in secret_params:
                    model_args_clean["extra_body"]["data_sources"][0]["parameters"][
                        "embedding_dependency"
                    ]["authentication"][field] = "*****"

    logging.debug(f"REQUEST BODY: {json.dumps(model_args_clean, indent=4)}")

    return model_args


async def promptflow_request(request):
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {app_settings.promptflow.api_key}",
        }
        # Adding timeout for scenarios where response takes longer to come back
        logging.debug(f"Setting timeout to {app_settings.promptflow.response_timeout}")
        async with httpx.AsyncClient(
            timeout=float(app_settings.promptflow.response_timeout)
        ) as client:
            pf_formatted_obj = convert_to_pf_format(
                request,
                app_settings.promptflow.request_field_name,
                app_settings.promptflow.response_field_name
            )
            # NOTE: This only support question and chat_history parameters
            # If you need to add more parameters, you need to modify the request body
            response = await client.post(
                app_settings.promptflow.endpoint,
                json={
                    app_settings.promptflow.request_field_name: pf_formatted_obj[-1]["inputs"][app_settings.promptflow.request_field_name],
                    "chat_history": pf_formatted_obj[:-1],
                },
                headers=headers,
            )
        resp = response.json()
        resp["id"] = request["messages"][-1]["id"]
        return resp
    except Exception as e:
        logging.error(f"An error occurred while making promptflow_request: {e}")


async def docupload_delete_by_tag(tagName, tagValue):
    if DOCUPLOAD_DELETE_BLOB_ON_CONVERSATION_DELETE:
        # Azure storage connection string
        connect_str = DOCUPLOAD_BLOB_CONNECTION_STRING
        # Create the BlobServiceClient object which will be used to create a container client
        blob_service_client = BlobServiceClient.from_connection_string(connect_str)

        # Remove blobs
        container_client = blob_service_client.get_container_client(DOCUPLOAD_AZURE_BLOB_CONTAINER)
        query = f"\"{tagName}\" = '{tagValue}'"
            # List blobs in the container

        blob_pages = container_client.find_blobs_by_tags(query)
        # Print the names of the blobs that were found
        for blob in blob_pages:
            container_client.delete_blob(blob)
            logging.debug(f"Conversation deleting - deleting {blob.name} tagged {tagName} = {tagValue}")

    
    if DOCUPLOAD_DELETE_INDEX_DOCUMENT_ON_CONVERSATION_DELETE:
        # remove from inxdex
        credential = AzureKeyCredential(AZURE_SEARCH_KEY)
        search_client = SearchClient(endpoint=AZURE_SEARCH_ENDPOINT, index_name=AZURE_SEARCH_INDEX, credential=credential)

        query = f"@{tagName} eq '{tagValue}'"
        results = search_client.search(search_text=query)

        # Delete documents based on their key
        count = 0
        for result in results:
            index_key = result[DOCUPLOAD_INDEX_DOCUMENT_KEY]
            search_client.delete_documents(documents=[{DOCUPLOAD_INDEX_DOCUMENT_KEY: index_key}] )
            count = count + 1
        logging.debug(f"Deleted {count} index document {DOCUPLOAD_INDEX_DOCUMENT_KEY} from {AZURE_SEARCH_INDEX} tagged with {query}")

async def send_chat_request(request_body, request_headers):
    filtered_messages = []
    messages = request_body.get("messages", [])
    for message in messages:
        if message.get("role") != 'tool':
            filtered_messages.append(message)
            
    request_body['messages'] = filtered_messages
    model_args = prepare_model_args(request_body, request_headers)

    try:
        azure_openai_client = init_openai_client()
        raw_response = await azure_openai_client.chat.completions.with_raw_response.create(**model_args)
        response = raw_response.parse()
        apim_request_id = raw_response.headers.get("apim-request-id") 
    except Exception as e:
        logging.exception("Exception in send_chat_request")
        raise e

    return response, apim_request_id


async def complete_chat_request(request_body, request_headers):
    if app_settings.base_settings.use_promptflow:
        response = await promptflow_request(request_body)
        history_metadata = request_body.get("history_metadata", {})
        return format_pf_non_streaming_response(
            response,
            history_metadata,
            app_settings.promptflow.response_field_name,
            app_settings.promptflow.citations_field_name
        )
    else:
        response, apim_request_id = await send_chat_request(request_body, request_headers)
        history_metadata = request_body.get("history_metadata", {})
        return format_non_streaming_response(response, history_metadata, apim_request_id)


async def stream_chat_request(request_body, request_headers):
    response, apim_request_id = await send_chat_request(request_body, request_headers)
    history_metadata = request_body.get("history_metadata", {})
    
    async def generate():
        async for completionChunk in response:
            yield format_stream_response(completionChunk, history_metadata, apim_request_id)

    return generate()


async def conversation_internal(request_body, request_headers):
    try:
        if app_settings.azure_openai.stream:
            result = await stream_chat_request(request_body, request_headers)
            response = await make_response(format_as_ndjson(result))
            response.timeout = None
            response.mimetype = "application/json-lines"
            return response
        else:
            result = await complete_chat_request(request_body, request_headers)
            return jsonify(result)

    except Exception as ex:
        logging.exception(ex)
        if hasattr(ex, "status_code"):
            return jsonify({"error": str(ex)}), ex.status_code
        else:
            return jsonify({"error": str(ex)}), 500
        



@bp.route("/conversation", methods=["POST"])
async def conversation():
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()
    print(f"request_json {request_json}")
    return await conversation_internal(request_json, request.headers)


@bp.route("/frontend_settings", methods=["GET"])
def get_frontend_settings():
    try:
        return jsonify(frontend_settings), 200
    except Exception as e:
        logging.exception("Exception in /frontend_settings")
        return jsonify({"error": str(e)}), 500
    
@bp.route("/document/index", methods=["POST"])
async def index_document():
    # Upload the created file
    try:
        data = await request.get_json()
        uniqueName = data['indexName']
        credential = AzureKeyCredential(AZURE_SEARCH_KEY)
        # Rest of the code...

        indexer_client = SearchIndexerClient(AZURE_SEARCH_ENDPOINT, credential)
        
        indexer = await indexer_client.get_indexer(DOCUPLOAD_AZURE_SEARCH_INDEXER)

        # Create a new indexer with a GUID added to the name
        new_indexer_name = indexer.name + '-' + uniqueName
        new_indexer = copy.deepcopy(indexer)
        new_indexer.name = new_indexer_name
        new_indexer.parameters.configuration.indexed_file_name_extensions = f".{uniqueName}"

        # create indexer clone - newly created indexers will automatically run
        await indexer_client.create_indexer(new_indexer)

        return jsonify({"indexer_name": new_indexer_name}), 200
    except Exception as e:
        abort(500, description=str(e))


@bp.route("/indexer/status", methods=["POST"])
async def get_indexer_status():
    try:
        try:
            request_json = await request.get_json()
            indexer_name = request_json.get('indexName')
        except Exception as e:
            logging.exception("Exception in /indexer/status request json")
            return jsonify({"error": str(e)}), 500
            
        credential = AzureKeyCredential(AZURE_SEARCH_KEY)
        indexer_client = SearchIndexerClient(AZURE_SEARCH_ENDPOINT, credential)
        indexer_status = await indexer_client.get_indexer_status(indexer_name)
        status = "notStarted"
        # Parse and add variables for each piece of information that the status check returns
        if (indexer_status.last_result is not None): 
            status = str(indexer_status.last_result.status)
        
        if (status == "success" or status == "transientFailure"):
            await indexer_client.delete_indexer(indexer_name)

        return jsonify({"status": status}), 200
    except Exception as e:
        logging.exception("Exception in /indexer/status")
        return jsonify({"error": str(e)}), 500  
    
def get_stream_size(stream):
    original_position = stream.tell()
    stream.seek(0, 2)  # Seek to the end of the stream
    size = stream.tell()
    stream.seek(original_position, 0)  # Return to the original position
    logging.info(size)
    return size

@bp.route("/document/upload", methods=["POST"])
async def upload_document():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user['user_principal_id']
    files = await request.files
    form = await request.form
    logging.debug('request_form_after_restart')
    
    file = files.get('file')
    filename = ''
    if file:
        filename = file.filename

    uniqueId = str(uuid.uuid4())
    conversation_id = ""
    file_size = get_stream_size(file)
    
    try :
        conversation_id = (await request.form)['conversationId']
    except Exception as e:
        try:
            # make sure cosmos is configured
            cosmos_conversation_client = init_cosmosdb_client()
            if not cosmos_conversation_client:
                raise Exception("CosmosDB is not configured or not working")

            # check for the conversation_id, if the conversation is not set, we will create a new one
            history_metadata = {}
            title = await generate_title([{'role': 'user', 'content': filename}])
            conversation_dict = await cosmos_conversation_client.create_conversation(user_id=user_id, title=title)
            conversation_id = conversation_dict['id']
            history_metadata['title'] = title
            history_metadata['date'] = conversation_dict['createdAt']
                
            ## Format the incoming message object in the "chat/completions" messages format
            ## then write it to the conversation history in cosmos
            messages = [{'role': 'user', 'content': filename}]
            createdMessageValue = await cosmos_conversation_client.create_message(
                    uuid=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    user_id=user_id,
                    input_message=messages[0]
                )
            if createdMessageValue == "Conversation not found":
                raise Exception("Conversation not found for the given conversation ID: " + conversation_id + ".")
            
            await cosmos_conversation_client.cosmosdb_client.close()
        
        except Exception as e:
            logging.exception("Exception in /document/upload")
            return jsonify({"error": str(e)}), 500
            

    # Azure storage connection string
    connect_str = DOCUPLOAD_BLOB_CONNECTION_STRING
    # Create the BlobServiceClient object which will be used to create a container client
    blob_service_client = BlobServiceClient.from_connection_string(connect_str)

    if 'file' not in await request.files:
        abort(400, description='No file part')
    file = (await request.files)['file']
    if file.filename == '':
        abort(400, description='No selected file')

    # Create a blob client using the local file name as the name for the blob
        
    blob_name = f"{user_id}/{conversation_id}/{file.filename}.{uniqueId}"
    if DOCUPLOAD_AZURE_BLOB_FOLDER:
        blob_name = f"{DOCUPLOAD_AZURE_BLOB_FOLDER}/{blob_name}"
    blob_client = blob_service_client.get_blob_client(DOCUPLOAD_AZURE_BLOB_CONTAINER, blob_name)

    # Define metadata
    metadata = {'user_id': user_id, 'conversation_id': conversation_id}
    tags = {'user_id': user_id, 'conversation_id': conversation_id}

    # Upload the created file
    try:
        blob_client.upload_blob(file.read(), metadata=metadata, tags=tags)
        return jsonify({"conversation_id": conversation_id, "index_id": uniqueId, "document_name": filename}), 200
    except Exception as e:
        logging.exception("Exception in /document/upload")
        return jsonify({"error": str(e)}), 500



## Conversation History API ##
@bp.route("/history/generate", methods=["POST"])
async def add_conversation():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        # make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # check for the conversation_id, if the conversation is not set, we will create a new one
        history_metadata = {}
        if not conversation_id:
            title = await generate_title(request_json["messages"])
            conversation_dict = await cosmos_conversation_client.create_conversation(
                user_id=user_id, title=title
            )
            conversation_id = conversation_dict["id"]
            history_metadata["title"] = title
            history_metadata["date"] = conversation_dict["createdAt"]

        ## Format the incoming message object in the "chat/completions" messages format
        ## then write it to the conversation history in cosmos
        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "user":
            createdMessageValue = await cosmos_conversation_client.create_message(
                uuid=str(uuid.uuid4()),
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
            if createdMessageValue == "Conversation not found":
                raise Exception(
                    "Conversation not found for the given conversation ID: "
                    + conversation_id
                    + "."
                )
        else:
            raise Exception("No user message found")

        await cosmos_conversation_client.cosmosdb_client.close()

        # Submit request to Chat Completions for response
        request_body = await request.get_json()
        history_metadata["conversation_id"] = conversation_id
        request_body["history_metadata"] = history_metadata
        return await conversation_internal(request_body, request.headers)

    except Exception as e:
        logging.exception("Exception in /history/generate")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/update", methods=["POST"])
async def update_conversation():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        # make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        # check for the conversation_id, if the conversation is not set, we will create a new one
        if not conversation_id:
            raise Exception("No conversation_id found")

        ## Format the incoming message object in the "chat/completions" messages format
        ## then write it to the conversation history in cosmos
        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "assistant":
            if len(messages) > 1 and messages[-2].get("role", None) == "tool":
                # write the tool message first
                await cosmos_conversation_client.create_message(
                    uuid=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    user_id=user_id,
                    input_message=messages[-2],
                )
            # write the assistant message
            await cosmos_conversation_client.create_message(
                uuid=messages[-1]["id"],
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
        else:
            raise Exception("No bot messages found")

        # Submit request to Chat Completions for response
        await cosmos_conversation_client.cosmosdb_client.close()
        response = {"success": True}
        return jsonify(response), 200

    except Exception as e:
        logging.exception("Exception in /history/update")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/message_feedback", methods=["POST"])
async def update_message():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    cosmos_conversation_client = init_cosmosdb_client()

    ## check request for message_id
    request_json = await request.get_json()
    message_id = request_json.get("message_id", None)
    message_feedback = request_json.get("message_feedback", None)
    try:
        if not message_id:
            return jsonify({"error": "message_id is required"}), 400

        if not message_feedback:
            return jsonify({"error": "message_feedback is required"}), 400

        ## update the message in cosmos
        updated_message = await cosmos_conversation_client.update_message_feedback(
            user_id, message_id, message_feedback
        )
        if updated_message:
            return (
                jsonify(
                    {
                        "message": f"Successfully updated message with feedback {message_feedback}",
                        "message_id": message_id,
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "error": f"Unable to update message {message_id}. It either does not exist or the user does not have access to it."
                    }
                ),
                404,
            )

    except Exception as e:
        logging.exception("Exception in /history/message_feedback")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/delete", methods=["DELETE"])
async def delete_conversation():
    ## get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)
    
    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        if DOCUPLOAD_ENABLED:
            await docupload_delete_by_tag("conversation_id", f"{conversation_id}")

        ## make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        ## delete the conversation messages from cosmos first
        deleted_messages = await cosmos_conversation_client.delete_messages(
            conversation_id, user_id
        )

        ## Now delete the conversation
        deleted_conversation = await cosmos_conversation_client.delete_conversation(
            user_id, conversation_id
        )

        await cosmos_conversation_client.cosmosdb_client.close()

        return (
            jsonify(
                {
                    "message": "Successfully deleted conversation and messages",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/delete")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/list", methods=["GET"])
async def list_conversations():
    offset = request.args.get("offset", 0)
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## make sure cosmos is configured
    cosmos_conversation_client = init_cosmosdb_client()
    if not cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    ## get the conversations from cosmos
    conversations = await cosmos_conversation_client.get_conversations(
        user_id, offset=offset, limit=25
    )
    await cosmos_conversation_client.cosmosdb_client.close()
    if not isinstance(conversations, list):
        return jsonify({"error": f"No conversations for {user_id} were found"}), 404

    ## return the conversation ids

    return jsonify(conversations), 200


@bp.route("/history/read", methods=["POST"])
async def get_conversation():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    ## make sure cosmos is configured
    cosmos_conversation_client = init_cosmosdb_client()
    if not cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    ## get the conversation object and the related messages from cosmos
    conversation = await cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    ## return the conversation id and the messages in the bot frontend format
    if not conversation:
        return (
            jsonify(
                {
                    "error": f"Conversation {conversation_id} was not found. It either does not exist or the logged in user does not have access to it."
                }
            ),
            404,
        )

    # get the messages for the conversation from cosmos
    conversation_messages = await cosmos_conversation_client.get_messages(
        user_id, conversation_id
    )

    ## format the messages in the bot frontend format
    messages = [
        {
            "id": msg["id"],
            "role": msg["role"],
            "content": msg["content"],
            "createdAt": msg["createdAt"],
            "feedback": msg.get("feedback"),
        }
        for msg in conversation_messages
    ]

    await cosmos_conversation_client.cosmosdb_client.close()
    return jsonify({"conversation_id": conversation_id, "messages": messages}), 200


@bp.route("/history/rename", methods=["POST"])
async def rename_conversation():
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    ## make sure cosmos is configured
    cosmos_conversation_client = init_cosmosdb_client()
    if not cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    ## get the conversation from cosmos
    conversation = await cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    if not conversation:
        return (
            jsonify(
                {
                    "error": f"Conversation {conversation_id} was not found. It either does not exist or the logged in user does not have access to it."
                }
            ),
            404,
        )

    ## update the title
    title = request_json.get("title", None)
    if not title:
        return jsonify({"error": "title is required"}), 400
    conversation["title"] = title
    updated_conversation = await cosmos_conversation_client.upsert_conversation(
        conversation
    )

    await cosmos_conversation_client.cosmosdb_client.close()
    return jsonify(updated_conversation), 200


@bp.route("/history/delete_all", methods=["DELETE"])
async def delete_all_conversations():
    ## get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    # get conversations for user
    try:
        ## make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        conversations = await cosmos_conversation_client.get_conversations(
            user_id, offset=0, limit=None
        )
        if not conversations:
            return jsonify({"error": f"No conversations for {user_id} were found"}), 404

        # delete each conversation
        for conversation in conversations:
            ## delete the conversation messages from cosmos first
            deleted_messages = await cosmos_conversation_client.delete_messages(
                conversation["id"], user_id
            )

            ## Now delete the conversation
            deleted_conversation = await cosmos_conversation_client.delete_conversation(
                user_id, conversation["id"]
            )

            if DOCUPLOAD_ENABLED:
                await docupload_delete_by_tag("conversaton_id", conversation['id'])

        await cosmos_conversation_client.cosmosdb_client.close()
        return (
            jsonify(
                {
                    "message": f"Successfully deleted conversation and messages for user {user_id}"
                }
            ),
            200,
        )

    except Exception as e:
        logging.exception("Exception in /history/delete_all")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/clear", methods=["POST"])
async def clear_messages():
    ## get the user id from the request headers
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    ## check request for conversation_id
    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        ## make sure cosmos is configured
        cosmos_conversation_client = init_cosmosdb_client()
        if not cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        ## delete the conversation messages from cosmos
        deleted_messages = await cosmos_conversation_client.delete_messages(
            conversation_id, user_id
        )

        return (
            jsonify(
                {
                    "message": "Successfully deleted messages in conversation",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/clear_messages")
        return jsonify({"error": str(e)}), 500


@bp.route("/history/ensure", methods=["GET"])
async def ensure_cosmos():
    if not app_settings.chat_history:
        return jsonify({"error": "CosmosDB is not configured"}), 404

    try:
        cosmos_conversation_client = init_cosmosdb_client()
        success, err = await cosmos_conversation_client.ensure()
        if not cosmos_conversation_client or not success:
            if err:
                return jsonify({"error": err}), 422
            return jsonify({"error": "CosmosDB is not configured or not working"}), 500

        await cosmos_conversation_client.cosmosdb_client.close()
        return jsonify({"message": "CosmosDB is configured and working"}), 200
    except Exception as e:
        logging.exception("Exception in /history/ensure")
        cosmos_exception = str(e)
        if "Invalid credentials" in cosmos_exception:
            return jsonify({"error": cosmos_exception}), 401
        elif "Invalid CosmosDB database name" in cosmos_exception:
            return (
                jsonify(
                    {
                        "error": f"{cosmos_exception} {app_settings.chat_history.database} for account {app_settings.chat_history.account}"
                    }
                ),
                422,
            )
        elif "Invalid CosmosDB container name" in cosmos_exception:
            return (
                jsonify(
                    {
                        "error": f"{cosmos_exception}: {app_settings.chat_history.conversations_container}"
                    }
                ),
                422,
            )
        else:
            return jsonify({"error": "CosmosDB is not working"}), 500


async def generate_title(conversation_messages):
    ## make sure the messages are sorted by _ts descending
    title_prompt = 'Summarize the conversation so far into a 4-word or less title. Do not use any quotation marks or punctuation. Respond with a json object in the format {{"title": string}}. Do not include any other commentary or description.'

    messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in conversation_messages
    ]
    messages.append({"role": "user", "content": title_prompt})

    try:
        azure_openai_client = init_openai_client(use_data=False)
        response = await azure_openai_client.chat.completions.create(
            model=app_settings.azure_openai.model, messages=messages, temperature=1, max_tokens=64
        )

        title = json.loads(response.choices[0].message.content)["title"]
        return title
    except Exception as e:
        return messages[-2]["content"]


app = create_app()
