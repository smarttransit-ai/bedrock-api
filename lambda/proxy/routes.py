"""Route identifiers shared by app.py, limits.py, and the transport modules.

These strings select protocol-specific behaviour at several branch points (output-cap
field, input-token estimation, pricing namespace). A bare-string typo at any one of them
fails silently rather than loudly — e.g. "response" instead of "responses" would fall
through to the InvokeModel branch of apply_output_cap, set ``max_tokens`` (which the
Responses API ignores) instead of ``max_output_tokens``, and leave the per-token output
cap unenforced. Constants make that a NameError instead.

Kept in their own module so the limits and transport layers can share them without
limits.py having to import bedrock.py — they are peers, and neither owns the route names.
"""

ROUTE_CONVERSE = "converse"
ROUTE_INVOKE = "invoke"
ROUTE_RESPONSES = "responses"
