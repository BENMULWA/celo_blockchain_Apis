"""Small helper package for Cardano integrations.

Provides HTTP-based Blockfrost helper so read-only operations work without
installing the Blockfrost SDK. For signing/submitting transactions you still
need `pycardano` and a chain context.
"""
__all__ = ["client"]
