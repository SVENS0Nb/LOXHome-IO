# Security

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub Security
Advisories for this repository. Do not include access tokens, passwords,
Miniserver structure files, serial numbers, or public Home Assistant URLs in a
public issue.

## Recommended deployment

- Create a dedicated, least-privileged Loxone user for Home Assistant.
- Prefer HTTPS with certificate verification enabled.
- Enable plain HTTP only on a trusted local network when HTTPS is unavailable.
- Keep Home Assistant, LOXHome I/O, and the Miniserver firmware updated.
- Treat Home Assistant backups as secrets because they contain integration
  credentials and authentication tokens.

LOXHome I/O diagnostics contain only aggregate counts and connection settings;
they intentionally omit credentials, tokens, hostnames, names, UUIDs, and
Miniserver serial numbers.
