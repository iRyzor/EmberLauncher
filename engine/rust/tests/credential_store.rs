//! Explicit local diagnostic using a separate, disposable entry, never the user's account.
#[test]
#[ignore = "requires an unlocked OS credential store; run explicitly on the test host"]
fn native_credential_store_round_trip_and_cleanup() {
    let id = uuid::Uuid::new_v4().to_string();
    let entry = keyring::Entry::new("com.emberlauncher.validation", &id).unwrap();
    // Public synthetic test data, no STOVE session and no network requests.
    entry
        .set_password("ember-synthetic-credential-check")
        .unwrap();
    struct Cleanup(keyring::Entry);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = self.0.delete_credential();
        }
    }
    let cleanup = Cleanup(entry);
    let reopened = keyring::Entry::new("com.emberlauncher.validation", &id).unwrap();
    assert_eq!(
        reopened.get_password().unwrap(),
        "ember-synthetic-credential-check"
    );
    cleanup.0.delete_credential().unwrap();
    assert!(matches!(
        reopened.get_password(),
        Err(keyring::Error::NoEntry)
    ));
}
