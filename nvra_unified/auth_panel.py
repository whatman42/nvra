"""DEPRECATED — not used by production single-user GUI."""
from __future__ import annotations
from .auth import create_account, login

def do_create_account(username: str, password: str):
    return create_account(username, password)

def do_login(username: str, password: str):
    return login(username, password)
