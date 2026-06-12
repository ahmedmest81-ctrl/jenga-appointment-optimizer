# Security

Jenga is a portfolio prototype, not a production medical system.

- Do not use real patient, guest, or customer data.
- Do not commit `.env`, OAuth tokens, API keys, or local database files.
- Rotate any credential that has previously been stored in a project copy.
- Treat calendar and notification integrations as development adapters until
  encryption, secret management, audit controls, and regulatory requirements
  have been implemented.

Please report security issues privately to the repository owner rather than
opening a public issue containing sensitive details.
