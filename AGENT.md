# Orb Agent Notes

## Model and Provider Selection

- Always use configuration and provider catalog data to select the model and provider.
- Never hardcode model ids, provider choices, or inline fallback defaults in runtime logic.
- If no valid configured model/provider is available, fail explicitly instead of silently inventing a fallback.

## Daemon Host

- Always run or restart the Orb daemon with host `0.0.0.0`.
