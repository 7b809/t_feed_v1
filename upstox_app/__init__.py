"""
upstox_app package.

Contains Upstox-specific service modules that consume the project's
token cache (token_tasks.service.token_service).

Currently exposes:
    - get_profile_status.get_profile  -> validate token via User Profile API
"""