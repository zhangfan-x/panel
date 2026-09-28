"""Wire protocol shared by the API process and the root helper.

Submodules: framing (length-prefixed JSON frames), fdpass (SCM_RIGHTS
primitives), messages (request/response models and helper error codes) and
client (API-side synchronous client with an asyncio wrapper).
"""
