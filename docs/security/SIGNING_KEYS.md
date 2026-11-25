# Signing keys: custody and procedure

Owner decision (25 November 2026): release-signing keys stay in the founders' custody. No
release key is created in a build container, a CI runner or any machine the founders do not
control. This document sets out how each key is made, kept, used, rotated and revoked.
`tests/readiness/test_signing_keys.py` checks the parts a test can check:
- no key material is tracked in git;
- the files that point at keys are ignored;
- the build reads keys only from outside the repository;
- every key the code reads is listed here.

**Status:** no release key exists yet. Every build so far is unsigned or debug-signed
(`docs/evidence/builds/ANDROID.md`). Requirement D1b.c (a signed release APK) waits on the
founders making the keys below.

## The keys

| Key | Signs | Read by | Where it lives | Who holds it |
|---|---|---|---|---|
| Watch app release key | The watch APK (`wearos` and `aosp` flavours) | `watch/app/build.gradle.kts`: `IRA_WATCH_KEYSTORE`, `IRA_WATCH_KEYSTORE_PASSWORD`, `IRA_WATCH_KEY_ALIAS`, `IRA_WATCH_KEY_PASSWORD`, from the environment or `watch/keystore.properties` (ignored) | An encrypted keystore on a founder-controlled machine; encrypted offline backups | Both founders (two-person rule for use) |
| Family app upload key (Android) | Uploads to Google Play; Play App Signing holds the app signing key | Expo / Gradle release build | As the watch key; separate keystore | Both founders |
| Apple distribution certificate and profile | The iOS family app | Xcode / EAS on macOS | The founders' Apple developer account and a macOS keychain | Account holder |
| IRA CARE OS platform keys (`releasekey`, `platform`, `shared`, `media`, `networkstack`, `sdk_sandbox`, `bluetooth`, `nfc`) | System APKs and the system image of a distributed OS build | `sign_target_files_apks` on the build host | Generated with AOSP's `development/tools/make_key` on the founder-controlled build host, kept offline | Both founders |
| Verified boot (AVB) key | vbmeta and the boot chain | `avbtool` / `BOARD_AVB_KEY_PATH` | Offline; a hardware-backed module when one is affordable | Both founders |
| OS OTA package key | A/B OTA packages | `ota_from_target_files` | As the platform keys | Both founders |
| OTA release manifest key | The server's signed OTA manifest (`care/ota.py`) | `IRA_OTA_MANIFEST_KEY` (base64 Ed25519 seed) in the server's environment (`deploy/.env`, never committed) | The server's secret store; an encrypted offline copy | Operator, under the founders |
| Server keys (`IRA_SECRET_KEY` and others) | Sign-in tokens, connector sealing | `deploy/.env` | `docs/ops/RUNBOOKS.md` §5 | Operator |
| Backup encryption key | Off-site backups | `deploy/.env` (`backup.sh`, `restore.sh`) | `docs/ops/RUNBOOKS.md` | Operator, with an offline copy held by the founders |

Development keys are separate from all of these and are never used to sign anything a person
outside the team installs:
- the Android debug keystore that Gradle makes;
- AOSP's public `test-keys` that a Cuttlefish development image uses;
- the OTA key a test generates.

AOSP's `test-keys` are public. Anyone can sign an update that a test-keys build accepts, so a
test-keys image must never reach a real person's watch.

## Generating a release key

1. Use a founder-controlled machine with full-disk encryption and an up-to-date operating
   system. Do not use a cloud build container, a CI runner or a shared machine.
2. Watch app: `keytool -genkeypair -v -keystore ira-watch-release.jks -alias ira-watch -keyalg RSA -keysize 4096 -validity 10000`.
   Choose long, random keystore and key passwords and record them in the founders' password
   manager. The family app's upload key is made the same way, as a separate keystore.
3. OS: on the build host, run `development/tools/make_key <name> '/C=GB/O=IRA CARE/CN=IRA CARE <name>'`
   for each platform key listed above, with a password, plus the AVB key
   (`openssl genrsa -out avb.pem 4096`, kept encrypted).
4. OTA manifest: `python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`,
   piped straight into the server's secret store. Publish only the public key (`care/ota.py`
   derives it), which the watch pins.
5. Record each key's fingerprint (`keytool -list -v`, `openssl pkey -pubout | sha256sum`) in the
   founders' key register: name, fingerprint, created, by whom, where its backups are. Record the
   fingerprint only, never the key.

## Keeping a key

- **Two copies offline:** encrypted (for example an encrypted disk image or `age`/`gpg`
  symmetric encryption), on two separate removable media in two separate places. Test that
  both copies restore once a year.
- **Passwords:** held in the founders' password manager, never stored next to the key.
- **No key in this repository, an issue, a chat, a log or a build log.** The ignore rules and
  `test_signing_keys.py` guard the repository; gitleaks scans every change.
- **Access:** both founders; one holds the key, the other the password for a release signing.

## Signing a release

1. Build the release from a tagged commit on `main` whose CI passed.
2. On the founder-controlled machine, set the `IRA_WATCH_*` variables in the shell (or a
   `watch/keystore.properties` that git ignores), build, then remove the variables and file.
3. Verify the signature and record it with the release notes: `apksigner verify --print-certs`
   for APKs, `avbtool verify_image` for OS images. The certificate digest must match the key
   register.
4. Publish the SHA-256 of every distributed artefact next to it, so a recipient can check it.

## Rotation and revocation

- **Watch and family apps:** Android's APK Signature Scheme v3 supports key rotation with a
  signed lineage (`apksigner rotate`). Rotate after any suspected exposure, and otherwise on a
  planned schedule. Google Play's app signing key is rotated through Play Console.
- **OTA manifest key:** generate a new seed and ship a watch update that pins both public keys.
  Switch the server to the new key, then ship an update that drops the old one.
- **OS platform and AVB keys:** a change of key is an OS release in its own right; watches in
  the field need an update signed with the old key that installs the new one. Plan it before
  the first distributed build.
- **Revocation:** there is no central revocation for APK keys. A compromised app key means
  rotating it and telling users. A compromised OTA key means rotating it at once. The server never offers a
  release with a lower rollback index than the device's (`care/ota.py`), and verified boot's
  rollback index refuses older images.

## If a key is exposed

1. Stop signing with it, and record when, how and who knew.
2. Rotate as above; for the OTA key, do it the same day.
3. Check what was signed with it since the last known-good point. Re-sign and re-publish
   checksums as needed.
4. Follow `docs/compliance/INCIDENT_PROCESS.md` for anyone affected, and record the incident.
5. If the key was ever committed, rotate it even after removing it: git history is not
   rewritten (repository rule) and the key must be treated as public.
