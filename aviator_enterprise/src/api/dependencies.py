"""FastAPI dependency providers."""
from __future__ import annotations

from fastapi import Request

from src.api.services import ServiceContainer


def get_services(request: Request) -> ServiceContainer:
    return request.app.state.services

