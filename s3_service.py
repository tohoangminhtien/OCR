"""
S3 Service module — all S3 Explorer API operations.
Functions: upload, file info, tree listing, metadata, tree building.
"""

import os
import sys
import json
import asyncio
import aiohttp
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

# Import from config
from server_config import (
    UPLOAD_URL, DOWNLOAD_BASE_URL, S3_LIST_API_URL,
    S3_PREFIX_DOCX, S3_PREFIX_PDF, S3_PREFIX_EXCEL, S3_PREFIX_PPTX,
    DEFAULT_CREATOR, DEFAULT_CREATOR_ID, DEFAULT_DATA_SOURCE,
    S3_EXPLORER_BASE_URL, S3_EXPLORER_USER_ID, S3_EXPLORER_GROUP_ID,
    S3_EXPLORER_GROUP_NAME, API_SECRET_KEY_DOWNLOAD, S3_BUCKET_NAME,
    get_http_session, logger
)

# Custom JSON encoder to handle datetime objects
class JsonEncoder(json.JSONEncoder):
    def default(self, obj):
        from datetime import datetime, date
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)


async def upload_file_to_s3(file_bytes: bytes, file_name: str, s3_prefix: str) -> Dict[str, Any]:
    """Upload file to S3 storage"""
    try:
        session = await get_http_session()
        
        # Log upload details for debugging
        logger.info(f"Uploading file: {file_name} ({len(file_bytes)} bytes) to prefix: {s3_prefix}")
        logger.info(f"Upload URL: {UPLOAD_URL}")
        
        # Prepare form data with proper content type based on file extension
        # quote_fields=False: giữ nguyên tên file (kể cả dấu cách + ký tự Unicode
        # như tiếng Việt). Mặc định True khiến aiohttp percent-encode filename
        # (VD 'BÁO GIÁ' -> 'B%C3%81O%20GI%C3%81'), làm S3 lưu key sai.
        data = aiohttp.FormData(quote_fields=False, charset="utf-8")
        
        # Determine content type based on file extension
        file_extension = file_name.lower().split('.')[-1] if '.' in file_name else ''
        content_type_map = {
            'html': 'text/html',
            'pdf': 'application/pdf',
            'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
            'csv': 'text/csv',
            'txt': 'text/plain',
            'json': 'application/json',
            'png': 'image/png',
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'gif': 'image/gif',
            'bmp': 'image/bmp'
        }
        content_type = content_type_map.get(file_extension, 'application/octet-stream')
        
        # Add file with proper content type - use S3 Explorer API format
        from io import BytesIO
        file_io = BytesIO(file_bytes)
        file_io.seek(0)  # Ensure we're at the beginning of the stream
        data.add_field('files', file_io, filename=file_name, content_type=content_type)
        data.add_field('prefix', s3_prefix)
        # Add metadata as JSON string (based on S3 Explorer API)
        metadata = {
            "creator": DEFAULT_CREATOR,
            "creator_id": DEFAULT_CREATOR_ID,
            "data_source": DEFAULT_DATA_SOURCE,
            "uploaded_at": datetime.now().isoformat()
        }
        data.add_field('metadata', json.dumps(metadata))
        
        # Log form data fields for debugging
        logger.info(f"Form fields: file={file_name}, prefix={s3_prefix}, creator={DEFAULT_CREATOR}")
        
        # Add authentication header for S3 Explorer API calls
        headers = {}
        if API_SECRET_KEY_DOWNLOAD:
            headers['api-secret-key-download'] = API_SECRET_KEY_DOWNLOAD
        
        try:
            async with session.post(UPLOAD_URL, data=data, headers=headers) as response:
                response_text = await response.text()
                logger.info(f"Upload response status: {response.status}")
                logger.info(f"Upload response headers: {dict(response.headers)}")
                logger.info(f"Upload response body: {response_text}")
                
                if response.status == 200:
                    try:
                        result = await response.json() if response.content_type == 'application/json' else {"message": response_text}
                    except:
                        result = {"message": response_text}

                    file_path = result.get('file_path') or f"{s3_prefix}{file_name}"
                        
                    logger.info(f"File uploaded successfully: {file_name}")
                    return {
                        "status": "success",
                        "file_path": file_path,
                        "download_url": f"/api/download?path={aiohttp.helpers.quote(file_path, safe='/')}",
                        "message": f"File {file_name} uploaded successfully"
                    }
                else:
                    logger.error(f"Upload failed: {response.status} - {response_text}")
                    return {
                        "status": "error",
                        "message": f"Upload failed: {response.status} - {response_text}"
                    }
        except aiohttp.ClientConnectorError as e:
            logger.error(f"Connection error: {e}")
            return {
                "status": "error", 
                "message": f"Connection error: Unable to connect to {UPLOAD_URL}. Check if s3-explorer-api service is running."
            }
        except asyncio.TimeoutError as e:
            logger.error(f"Timeout error: {e}")
            return {
                "status": "error",
                "message": f"Timeout error: Request to {UPLOAD_URL} timed out after 30s"
            }
        except aiohttp.ClientError as e:
            logger.error(f"HTTP client error: {e}")
            return {
                "status": "error",
                "message": f"HTTP client error: {str(e)}"
            }
                
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return {
            "status": "error",
            "message": f"Upload error: {str(e)}"
        }


async def get_s3_file_info(file_path: str) -> Optional[Dict[str, Any]]:
    """Get file info from S3 Explorer API to get proper download URL"""
    try:
        # First, list files in the directory containing the file
        # Extract directory from file path
        dir_path = str(Path(file_path).parent)
        if not dir_path.endswith('/'):
            dir_path += '/'
        
        # Get HTTP session
        session = await get_http_session()
        
        # Construct API URL to list files in directory
        list_url = (
            f"{S3_EXPLORER_BASE_URL}/files"
            f"?cache=false"
            f"&displayPrefixes=%5B%5D"
            f"&prefix={aiohttp.helpers.quote(dir_path, safe='')}"
            f"&userId={S3_EXPLORER_USER_ID}"
            f"&groupId={S3_EXPLORER_GROUP_ID}"
            f"&groupname={S3_EXPLORER_GROUP_NAME}"
        )
        headers = {
            "api-secret-key-download": API_SECRET_KEY_DOWNLOAD
        }
        
        async with session.get(list_url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                
                # Find the array in the response
                items = []
                for value in data.values():
                    if isinstance(value, list):
                        items = value
                        break
                
                # Find the file we're looking for
                for item in items:
                    if item.get('name') == Path(file_path).name:
                        # Return download URL from file info
                        return {
                            "download_url": f"{DOWNLOAD_BASE_URL}?path={aiohttp.helpers.quote(file_path, safe='/')}",
                            "file_info": item
                        }
                
                return None
            else:
                return None
                
    except Exception as e:
        logger.error(f"Error getting S3 file info: {e}")
        return None


async def fetch_file_metadata(session, file_key: str) -> Optional[Dict[str, Any]]:
    """Fetch metadata from a single file's .metadata.json sidecar on S3 (used as fallback)."""
    try:
        metadata_path = f"{file_key}.metadata.json"
        download_url = f"{DOWNLOAD_BASE_URL}?path={aiohttp.helpers.quote(metadata_path, safe='/')}"
        
        # Add authentication header for S3 Explorer API calls
        headers = {}
        if API_SECRET_KEY_DOWNLOAD:
            headers['api-secret-key-download'] = API_SECRET_KEY_DOWNLOAD
        
        async with session.get(download_url, headers=headers) as response:
            if response.status == 200:
                text = await response.text()
                try:
                    metadata_json = json.loads(text)
                    return metadata_json.get('metadataAttributes', {})
                except (json.JSONDecodeError, ValueError):
                    return None
            else:
                return None
    except Exception:
        return None


def apply_metadata_filter(
    metadata: Optional[Dict[str, Any]],
    metadata_filter: Dict[str, Any],
    filter_mode: str = "notEquals"
) -> bool:
    """Check if a file's metadata passes the filter.
    
    Returns True if the file should be INCLUDED in the results.
    """
    if metadata is None:
        return True
    
    for key, expected_value in metadata_filter.items():
        actual_value = metadata.get(key, None)
        
        # Treat whitespace-only values as empty/missing
        if isinstance(actual_value, str) and actual_value.strip() == '':
            actual_value = None
        
        if filter_mode == "equals":
            if actual_value != expected_value:
                return False
        elif filter_mode == "notEquals":
            if actual_value is not None and actual_value == expected_value:
                return False
    
    return True


async def fetch_all_metadata(
    session,
    file_keys: List[str]
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Fetch metadata for all files, using batch API with individual fallback.
    
    Strategy:
    1. Try batch API (single HTTP call) -> S3 Explorer reads all metadata server-side
    2. If batch API unavailable, fall back to individual fetches with concurrency limit
    """
    # Try batch API first (1 HTTP call instead of N)
    try:
        batch_url = f"{S3_EXPLORER_BASE_URL}/files/metadata-batch"
        payload = {"keys": file_keys}
        
        # Add authentication header
        headers = {}
        if API_SECRET_KEY_DOWNLOAD:
            headers['api-secret-key-download'] = API_SECRET_KEY_DOWNLOAD
        
        async with session.post(batch_url, json=payload, headers=headers) as response:
            if response.status == 200:
                batch_result = await response.json()
                logger.info(f"Batch metadata API returned {len(batch_result)} results")
                return batch_result
            else:
                logger.warning(f"Batch metadata API returned {response.status}, falling back to individual fetches")
    except Exception as e:
        logger.warning(f"Batch metadata API error: {e}, falling back to individual fetches")
    
    # Fallback: individual fetches with semaphore
    logger.info(f"Falling back to individual metadata fetches for {len(file_keys)} files")
    semaphore = asyncio.Semaphore(10)
    
    async def fetch_with_semaphore(file_key):
        async with semaphore:
            metadata = await fetch_file_metadata(session, file_key)
            return file_key, metadata
    
    tasks = [fetch_with_semaphore(k) for k in file_keys]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    metadata_map = {}
    for result in results:
        if isinstance(result, Exception):
            logger.warning(f"Metadata fetch failed: {result}")
            continue
        file_key, metadata = result
        metadata_map[file_key] = metadata
    
    return metadata_map


async def list_s3_tree_recursive(
    prefix: str,
    metadata_filter: Optional[Dict[str, Any]] = None,
    filter_mode: str = "notEquals"
) -> str:
    """List S3 directory tree using the correct API format, with optional metadata filtering.
    
    Args:
        prefix: S3 prefix/path to list.
        metadata_filter: Optional dict of {metadata_field: value} to filter files.
        filter_mode: "equals" to include only matching, "notEquals" to exclude matching.
    """
    try:
        logger.info(f"Listing S3 tree for prefix: {prefix}")
        if metadata_filter:
            logger.info(f"Metadata filter: {metadata_filter} (mode: {filter_mode})")
        
        # Normalize path - ensure it ends with '/'
        normalized_path = prefix.strip()
        if not normalized_path.endswith('/'):
            normalized_path += '/'
        
        # Get HTTP session
        session = await get_http_session()
        
        # Construct API URL matching the JavaScript logic
        list_url = (
            f"{S3_EXPLORER_BASE_URL}/files"
            f"?cache=false"
            f"&displayPrefixes=%5B%5D"  # encoded []
            f"&prefix={aiohttp.helpers.quote(normalized_path, safe='')}"
            f"&userId={S3_EXPLORER_USER_ID}"
            f"&groupId={S3_EXPLORER_GROUP_ID}"
            f"&groupname={S3_EXPLORER_GROUP_NAME}"
        )
        
        logger.info(f"API URL: {list_url}")
        
        # Add authentication header
        headers = {}
        if API_SECRET_KEY_DOWNLOAD:
            headers['api-secret-key-download'] = API_SECRET_KEY_DOWNLOAD
        
        try:
            async with session.get(list_url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Find the array in the response (matching JS logic)
                    items = []
                    for value in data.values():
                        if isinstance(value, list):
                            items = value
                            break
                    
                    # Map items to children format
                    children = []
                    for item in items:
                        children.append({
                            'name': item.get('name', ''),
                            'type': 'folder' if item.get('isFolder', False) else 'file',
                            'path': item.get('key', '')
                        })
                    
                    # Apply metadata filtering if requested
                    if metadata_filter and isinstance(metadata_filter, dict) and len(metadata_filter) > 0:
                        # Separate folders (always included) and files (to be filtered)
                        folders = [c for c in children if c['type'] == 'folder']
                        files = [c for c in children if c['type'] == 'file']
                        
                        if files:
                            logger.info(f"Fetching metadata for {len(files)} files to apply filter...")
                            
                            # Fetch all metadata (batch API with individual fallback)
                            file_keys = [f['path'] for f in files]
                            metadata_map = await fetch_all_metadata(session, file_keys)
                            
                            # Filter files based on metadata
                            filtered_files = []
                            for file_child in files:
                                file_metadata = metadata_map.get(file_child['path'])
                                if apply_metadata_filter(file_metadata, metadata_filter, filter_mode):
                                    filtered_files.append(file_child)
                            
                            excluded_count = len(files) - len(filtered_files)
                            logger.info(f"Metadata filter applied: {len(filtered_files)} files kept, {excluded_count} files excluded")
                            
                            children = folders + filtered_files
                    
                    # Build result structure
                    folder_name = normalized_path.rstrip('/').split('/')[-1] if normalized_path.rstrip('/') else 'root'
                    result = {
                        'name': folder_name,
                        'path': normalized_path,
                        'children': children
                    }
                    
                    return json.dumps(result, ensure_ascii=False, indent=2)
                    
                elif response.status == 404:
                    logger.warning(f"S3 API not found: {list_url}")
                    return json.dumps({
                        'error': 'API endpoint not found',
                        'url': list_url,
                        'status': 404
                    }, ensure_ascii=False, indent=2)
                    
                else:
                    error_text = await response.text()
                    logger.error(f"S3 API error: {response.status} - {error_text}")
                    return json.dumps({
                        'error': f'API Error: {response.status}',
                        'message': error_text,
                        'url': list_url
                    }, ensure_ascii=False, indent=2)
                    
        except aiohttp.ClientError as e:
            logger.error(f"HTTP client error: {e}")
            return json.dumps({
                'error': 'Network error',
                'message': str(e),
                'url': list_url
            }, ensure_ascii=False, indent=2)
            
    except Exception as e:
        logger.error(f"Error listing S3 tree: {e}")
        return json.dumps({
            'error': 'Unexpected error',
            'message': str(e)
        }, ensure_ascii=False, indent=2)


async def get_files_metadata(file_paths: List[str]) -> str:
    """Fetch metadata for a list of S3 files.
    
    Returns metadata attributes (status, effectiveDate, expirationDate, etc.)
    for each file, plus a computed 'is_valid' flag based on dates vs current date.
    
    Args:
        file_paths: List of S3 file keys to check metadata for.
    """
    try:
        from datetime import date
        today = date.today().isoformat()  # "YYYY-MM-DD"
        
        session = await get_http_session()
        
        # Fetch metadata for all files
        metadata_map = await fetch_all_metadata(session, file_paths)
        
        results = {}
        for file_path in file_paths:
            metadata = metadata_map.get(file_path)
            
            if metadata is None:
                results[file_path] = {
                    "has_metadata": False,
                    "metadata": None
                }
                continue
            
            # Compute validity based on dates
            effective_date = (metadata.get("effectiveDate") or "").strip()
            expiration_date = (metadata.get("expirationDate") or "").strip()
            status = (metadata.get("status") or "").strip()
            
            is_effective = True   # default: effective if no date set
            is_expired = False    # default: not expired if no date set
            
            if effective_date:
                try:
                    is_effective = effective_date <= today
                except (TypeError, ValueError):
                    is_effective = True  # can't parse → assume effective
            
            if expiration_date:
                try:
                    is_expired = expiration_date < today
                except (TypeError, ValueError):
                    is_expired = False  # can't parse → assume not expired
            
            is_valid = is_effective and not is_expired and status != "het_hieu_luc"
            
            results[file_path] = {
                "has_metadata": True,
                "metadata": metadata,
                "computed": {
                    "is_valid": is_valid,
                    "is_effective": is_effective,
                    "is_expired": is_expired,
                    "checked_against": today
                }
            }
        
        return json.dumps(results, ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.error(f"Error fetching files metadata: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def search_files_by_name(
    query: str,
    root_prefix: str = "",
    limit: int = 20
) -> str:
    """Search for files by name/keyword using MeiliSearch (text search, not vector search).
    
    Args:
        query: Search keyword (file name, document code, etc.)
        root_prefix: Optional prefix to filter results within a folder (e.g. "kho_doi_ngoai")
        limit: Maximum number of results to return (default 20)
    """
    try:
        session = await get_http_session()
        
        # Normalize root_prefix: strip slashes for consistent matching
        filter_prefix = root_prefix.strip('/') if root_prefix else ""
        
        # Request more results from MeiliSearch since we'll post-filter by prefix
        fetch_limit = limit * 5 if filter_prefix else limit
        
        # Call S3 Explorer MeiliSearch endpoint
        # NOTE: Do NOT pass rootPrefix to the API — basicSearch concatenates it
        # into the query text (rootPrefix + query), which causes bad fuzzy matches.
        # Instead, we search by query only and post-filter by prefix.
        search_url = (
            f"{S3_EXPLORER_BASE_URL}/search-db"
            f"?query={aiohttp.helpers.quote(query, safe='')}"
            f"&rootPrefix="
            f"&limit={fetch_limit}"
            f"&offset=0"
            f"&userId={S3_EXPLORER_USER_ID}"
            f"&groups={aiohttp.helpers.quote(json.dumps([S3_EXPLORER_GROUP_NAME]), safe='')}"
        )
        
        logger.info(f"🔍 Searching files by name: query='{query}', filter_prefix='{filter_prefix}'")
        
        # Add authentication header
        headers = {}
        if API_SECRET_KEY_DOWNLOAD:
            headers['api-secret-key-download'] = API_SECRET_KEY_DOWNLOAD
        
        async with session.get(search_url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                
                # Extract results from response
                hits = []
                
                if isinstance(data, dict) and 'result' in data:
                    hits = data.get('result', [])
                elif isinstance(data, list):
                    hits = data
                
                # Post-filter and build results
                results = []
                for hit in hits:
                    key = hit.get('key', '')
                    is_folder = bool(hit.get('idFolder', False))
                    
                    # Skip folders
                    if is_folder:
                        continue
                    
                    # Skip keys that start with the bucket name (not valid download paths)
                    if S3_BUCKET_NAME and key.startswith(S3_BUCKET_NAME + '/'):
                        continue
                    
                    # Filter by root_prefix if specified
                    if filter_prefix and not key.startswith(filter_prefix + '/'):
                        continue
                    
                    file_info = {
                        'name': hit.get('name', ''),
                        'path': key,
                        'size': hit.get('size', 0),
                        'lastModified': hit.get('lastModified', ''),
                        'contentType': hit.get('contentType', ''),
                    }
                    results.append(file_info)
                    
                    # Stop once we have enough filtered results
                    if len(results) >= limit:
                        break
                
                total_hits = data.get('estimatedTotalHits', len(results)) if isinstance(data, dict) else len(results)
                
                logger.info(f"✅ Search returned {len(results)} filtered results (raw hits: {len(hits)}, estimated total: {total_hits})")
                
                return json.dumps({
                    'query': query,
                    'root_prefix': filter_prefix,
                    'total_results': len(results),
                    'results': results
                }, ensure_ascii=False, indent=2)
                
            else:
                error_text = await response.text()
                logger.error(f"Search API error: {response.status} - {error_text}")
                return json.dumps({
                    'error': f'Search API Error: {response.status}',
                    'message': error_text
                }, ensure_ascii=False, indent=2)
                
    except Exception as e:
        logger.error(f"Error searching files: {e}")
        return json.dumps({'error': str(e)}, ensure_ascii=False)


def build_tree_structure(prefix: str, file_list: List[Dict]) -> str:
    """Build tree structure from file list"""
    try:
        if not file_list:
            return f"S3 Explorer - {prefix}/\n-- (empty)"
        
        # Group files by directory
        tree = {}
        
        for file_info in file_list:
            file_path = file_info.get('path', file_info.get('key', ''))
            if not file_path:
                continue
                
            # Remove prefix from path
            if file_path.startswith(prefix):
                relative_path = file_path[len(prefix):].lstrip('/')
            else:
                relative_path = file_path
            
            if not relative_path:
                continue
                
            # Split path into parts
            parts = relative_path.split('/')
            current = tree
            
            # Build nested structure
            for i, part in enumerate(parts):
                if i == len(parts) - 1:  # It's a file
                    if 'files' not in current:
                        current['files'] = []
                    
                    file_size = file_info.get('size', 0)
                    file_modified = file_info.get('last_modified', '')
                    
                    current['files'].append({
                        'name': part,
                        'size': file_size,
                        'modified': file_modified
                    })
                else:  # It's a directory
                    if 'dirs' not in current:
                        current['dirs'] = {}
                    if part not in current['dirs']:
                        current['dirs'][part] = {}
                    current = current['dirs'][part]
        
        # Convert tree to string representation
        result = f"S3 Explorer - {prefix}/\n"
        result += format_tree_node(tree, "", True)
        
        return result
        
    except Exception as e:
        logger.error(f"Error building tree structure: {e}")
        return f"S3 Explorer - {prefix}/\n-- Error building tree: {str(e)}"


def format_tree_node(node: Dict, indent: str, is_last: bool) -> str:
    """Format tree node as string"""
    result = ""
    
    # Handle directories
    dirs = node.get('dirs', {})
    files = node.get('files', [])
    
    total_items = len(dirs) + len(files)
    current_item = 0
    
    # Sort directories and files
    sorted_dirs = sorted(dirs.items())
    sorted_files = sorted(files, key=lambda x: x['name'])
    
    # Add directories
    for dir_name, dir_content in sorted_dirs:
        current_item += 1
        is_last_item = current_item == total_items
        
        if is_last_item:
            result += f"{indent}-- {dir_name}/\n"
            new_indent = indent + "    "
        else:
            result += f"{indent}+- {dir_name}/\n"
            new_indent = indent + "|   "
            
        result += format_tree_node(dir_content, new_indent, True)
    
    # Add files
    for file_info in sorted_files:
        current_item += 1
        is_last_item = current_item == total_items
        
        file_name = file_info['name']
        file_size = file_info.get('size', 0)
        
        # Format file size
        if file_size > 0:
            if file_size < 1024:
                size_str = f"{file_size}B"
            elif file_size < 1024 * 1024:
                size_str = f"{file_size/1024:.1f}KB"
            elif file_size < 1024 * 1024 * 1024:
                size_str = f"{file_size/(1024*1024):.1f}MB"
            else:
                size_str = f"{file_size/(1024*1024*1024):.1f}GB"
            
            file_display = f"{file_name} ({size_str})"
        else:
            file_display = file_name
        
        if is_last_item:
            result += f"{indent}-- {file_display}\n"
        else:
            result += f"{indent}+- {file_display}\n"
    
    return result


def generate_mock_tree_structure(prefix: str) -> str:
    """Generate mock tree structure when API is not available"""
    return f"""S3 Explorer - {prefix}/
+- documents/
|   +- reports/
|   |   +- monthly_report.xlsx (2.3MB)
|   |   +- quarterly_analysis.pdf (1.8MB)
|   |   +- annual_summary.docx (1.2MB)
|   +- presentations/
|   |   +- board_meeting.pptx (5.4MB)
|   |   +- strategy_2024.pptx (3.7MB)
|   |   +- product_launch.pptx (4.1MB)
|   +- contracts/
|       +- vendor_agreement.pdf (890KB)
|       +- service_contract.pdf (1.1MB)
+- data/
|   +- sales_data.csv (456KB)
|   +- customer_feedback.xlsx (2.1MB)
|   +- market_analysis.xlsx (1.7MB)
+- images/
    +- logo.png (234KB)
    +- banner.jpg (1.2MB)
    +- screenshots/
        +- dashboard.png (567KB)
        +- mobile_app.png (432KB)

Note: This is mock data. S3 API endpoint may not be configured.
API URL: {S3_LIST_API_URL}"""

import json
import base64
from typing import Any, Optional


def extract_bytes_from_json_response(json_data: Any) -> Optional[bytes]:
    """Extract bytes from S3 Explorer JSON response"""
    if isinstance(json_data, dict):
        if 'bytes' in json_data:
            return json_data['bytes']
        elif 'content' in json_data:
            return base64.b64decode(json_data['content'])
        elif 'data' in json_data:
            return json_data['data']
    return None


async def download_s3_or_url_bytes(file_path: str) -> bytes:
    """Download raw bytes from S3 path or direct URL."""
    download_url = file_path if file_path.startswith('http') else f"{S3_EXPLORER_BASE_URL}/download?path={aiohttp.helpers.quote(file_path, safe='/')}"
    session = await get_http_session()

    # Add authentication header for S3 Explorer API calls
    headers = {}
    if not file_path.startswith('http') and API_SECRET_KEY_DOWNLOAD:
        headers['api-secret-key-download'] = API_SECRET_KEY_DOWNLOAD
        
    logger.info(f"Download URL {download_url} headers {headers}")
    async with session.get(download_url, headers=headers) as response:
        if response.status != 200:
            error_text = await response.text()
            raise ValueError(f"Failed to download: HTTP {response.status}: {error_text}")

        content_type = response.headers.get('Content-Type', '')
        if 'application/json' in content_type.lower():
            json_data = await response.json()
            file_bytes = extract_bytes_from_json_response(json_data)
            if file_bytes is None:
                raise ValueError(f"S3 Explorer returned JSON instead of bytes: {json_data}")
            return file_bytes

        return await response.read()
