# Account privacy

Sign-in is approved by scanning a QR code in the official STOVE mobile app. Ember does not collect your STOVE password or import the official launcher's cookies or saved credentials.

Account and game session tokens stay in the private Rust engine. The interface receives the QR image and public status information, never the tokens.

## Remember me

The default is off: session credentials stay in memory and are discarded when the launcher closes.

When enabled, Ember stores the refresh credential and the account identifier needed to check identity continuity in the operating system's credential manager: Secret Service on Linux, Credential Manager on Windows, and Keychain on macOS. There is no plaintext configuration-file fallback. The access token, game session and password are not saved there.

Turning the option off removes the saved credential while keeping the current in-memory session. Disconnecting the account removes the saved credential and disables automatic restoration. If the credential manager cannot be accessed, Ember displays an error; a failed deletion may need to be completed in the system credential manager.

Restoration is attempted once automatically when the launcher opens. Network errors do not trigger constant retries. The restore flow has automated tests; wider real-account validation remains pending.

The system credential manager does not protect against malicious software already running with access to your operating-system account.

## Other local data

Settings contain installation paths and preferences. The system timezone and regional locale supply connection metadata automatically. Cached downloads and official news are stored locally. Ember contacts STOVE services to authenticate, download game files and fetch public news.

## Sharing an issue

Never publish QR codes, tokens, cookies, credential-store contents, raw authentication traffic or registry exports. Review logs and screenshots for personal information and local paths before uploading them. See [Security reporting](../SECURITY.md) for handling a suspected credential exposure.
