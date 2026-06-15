"""
Multi-user data operations helper module
Provides functions to integrate user_id into database queries
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

LOGGER = logging.getLogger(__name__)


def inject_user_id_into_insert(data: dict[str, Any], user_id: str | UUID) -> dict[str, Any]:
    """
    Inject user_id into insert payload.
    
    Args:
        data: Original data dictionary
        user_id: User's UUID
    
    Returns:
        Data dictionary with user_id added
    """
    result = data.copy()
    result["user_id"] = str(user_id)
    return result


def build_user_filter_clause(user_id: str | UUID, table_alias: str = "") -> str:
    """
    Build SQL WHERE clause for user_id filtering.
    
    Args:
        user_id: User's UUID
        table_alias: Optional table alias (e.g., "t" for "t.user_id")
    
    Returns:
        SQL WHERE clause string
    """
    prefix = f"{table_alias}." if table_alias else ""
    return f"{prefix}user_id = '{str(user_id)}'"


def log_user_data_access(user_id: str | UUID, action: str, table: str, details: str = "") -> None:
    """
    Log user data access for audit trail.
    
    Args:
        user_id: User's UUID
        action: Action type (SELECT, INSERT, UPDATE, DELETE)
        table: Table name
        details: Additional details
    """
    LOGGER.info(
        "USER_DATA_ACCESS: user_id=%s, action=%s, table=%s %s",
        str(user_id)[:8],  # Log first 8 chars of UUID for brevity
        action,
        table,
        details
    )


def validate_user_ownership(row_user_id: str | UUID, request_user_id: str | UUID) -> bool:
    """
    Validate that a row belongs to the requesting user.
    
    Args:
        row_user_id: User ID from the database row
        request_user_id: User ID making the request
    
    Returns:
        True if ownership is valid, False otherwise
    """
    return str(row_user_id) == str(request_user_id)
