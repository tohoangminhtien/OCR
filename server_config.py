"""
Server configuration: environment variables, constants, HTTP settings.
Centralized config for the CEO Assistant MCP Server.
"""

import os
import sys
import asyncio
import aiohttp
from pathlib import Path
from datetime import datetime

# Load environment variables
from dotenv import load_dotenv

# Add current directory to Python path
sys.path.insert(0, str(Path(__file__).parent))

# Load environment variables
load_dotenv()

# Setup logging
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "server.log", encoding="utf-8")
    ]
)

logger = logging.getLogger(__name__)

# ============================================================================
# Core Configuration
# ============================================================================

UPLOAD_URL = os.getenv('UPLOAD_URL', 'http://s3-explorer-api:3001/api/upload')
DOWNLOAD_BASE_URL = os.getenv('DOWNLOAD_BASE_URL', 'http://s3-explorer-api:3001/api/download')
S3_LIST_API_URL = os.getenv('S3_LIST_API_URL', f"{os.getenv('DOWNLOAD_BASE_URL', 'https://your-download-url.com')}/list")
S3_PREFIX_DOCX = os.getenv('S3_PREFIX_DOCX', 'documents/docx/')
S3_PREFIX_PDF = os.getenv('S3_PREFIX_PDF', 'documents/pdf/')
S3_PREFIX_EXCEL = os.getenv('S3_PREFIX_EXCEL', 'documents/excel/')
S3_PREFIX_PPTX = os.getenv('S3_PREFIX_PPTX', 'documents/pptx/')

DEFAULT_CREATOR = os.getenv('DEFAULT_CREATOR', 'CEO Assistant')
DEFAULT_CREATOR_ID = os.getenv('DEFAULT_CREATOR_ID', 'ceo-assistant')
DEFAULT_DATA_SOURCE = os.getenv('DEFAULT_DATA_SOURCE', 'MCP Server')
TAVILY_API_KEY = os.getenv('TAVILY_API_KEY')

# S3 Explorer API Configuration
S3_EXPLORER_BASE_URL = os.getenv('S3_EXPLORER_BASE_URL', 'http://s3-explorer-api:3001/api')
S3_EXPLORER_USER_ID = os.getenv('S3_EXPLORER_USER_ID', '0299b2b0-980a-4d5c-8692-bc69e3b576e2')
S3_EXPLORER_GROUP_ID = os.getenv('S3_EXPLORER_GROUP_ID', '1bb66647-6409-45bf-aff4-19682c3848b9')
S3_EXPLORER_GROUP_NAME = os.getenv('S3_EXPLORER_GROUP_NAME', 'Master_admin')

# Download API Secret Key (required for kho_doi_noi and protected paths)
API_SECRET_KEY_DOWNLOAD = os.getenv('API_SECRET_KEY_DOWNLOAD', '')

# Internal secret sent as x-auth-secret-key to S3 Explorer email-scheduler APIs
S3_EXPLORER_INTERNAL_SECRET = os.getenv('S3_EXPLORER_INTERNAL_SECRET', '')

# Public base URL of s3-explorer reachable by end users (not the internal S3_EXPLORER_BASE_URL).
# Used only by SIGN_FILE_VIEW_URL to build signed /api/file/view links.
S3_EXPLORER_PUBLIC_URL = os.getenv('S3_EXPLORER_PUBLIC_URL', '')

# S3 Bucket Name (used to filter out bucket-name-prefixed keys from search results)
S3_BUCKET_NAME = os.getenv('S3_STORAGE_BUCKET_NAME', 'hoaphat-cagent-cmcts-ai')

# Server Configuration
MCP_SERVER_HOST = os.getenv('MCP_SERVER_HOST', '0.0.0.0')
MCP_SERVER_PORT = int(os.getenv('MCP_SERVER_PORT', '8069'))
MAX_CONCURRENT_REQUESTS = int(os.getenv('MAX_CONCURRENT_REQUESTS', '30'))

# HTTP Configuration
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '30'))
HTTP_CONNECTION_LIMIT = int(os.getenv('HTTP_CONNECTION_LIMIT', '100'))
HTTP_CONNECTION_LIMIT_PER_HOST = int(os.getenv('HTTP_CONNECTION_LIMIT_PER_HOST', '30'))
DNS_CACHE_TTL = int(os.getenv('DNS_CACHE_TTL', '300'))
HTTP_CONNECT_TIMEOUT = int(os.getenv('HTTP_CONNECT_TIMEOUT', '5'))
HTTP_READ_TIMEOUT = int(os.getenv('HTTP_READ_TIMEOUT', '10'))

# Global HTTP session
http_session = None


async def get_http_session():
    """Get or create HTTP session with optimized settings"""
    global http_session
    if http_session is None or http_session.closed:
        connector = aiohttp.TCPConnector(
            limit=HTTP_CONNECTION_LIMIT,
            limit_per_host=HTTP_CONNECTION_LIMIT_PER_HOST,
            ttl_dns_cache=DNS_CACHE_TTL,
            use_dns_cache=True,
        )
        
        timeout = aiohttp.ClientTimeout(
            total=REQUEST_TIMEOUT,
            connect=HTTP_CONNECT_TIMEOUT,
            sock_read=HTTP_READ_TIMEOUT
        )
        
        http_session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout
        )
    
    return http_session
