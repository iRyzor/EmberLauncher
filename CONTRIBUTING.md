# Contributing

Bug reports, platform testing and focused improvements are welcome. Ember currently supports only Chaos Zero Nightmare.

## Report a problem

Please include:

- Ember version and package type.
- Operating system and architecture; Wine version and runner on Linux.
- Steps to reproduce, expected behavior and the exact visible error.
- A screenshot or a short sanitized log if useful.

Do not include QR codes, account tokens, cookies or private authentication captures. Handle suspected credential exposure as described in [SECURITY.md](SECURITY.md).

## Make a change

1. Describe the problem in an issue, especially for a substantial change.
2. Keep pull requests focused and explain the resulting behavior.
3. Follow the existing Rust and React/TypeScript patterns.
4. Keep English and Spanish UI text in sync.
5. Run the relevant checks in [Building](docs/BUILDING.md) and state which environments were tested.

Do not claim platform or game compatibility based only on a successful compilation. Tests should use synthetic account data, never real credentials. Do not commit generated packages, local prefixes or research captures.
