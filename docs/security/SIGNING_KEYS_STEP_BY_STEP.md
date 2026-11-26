# Signing keys, step by step (for the founders)

This is the hands-on companion to [`SIGNING_KEYS.md`](SIGNING_KEYS.md), which sets out the custody
policy. Follow these steps on a computer you control. Never run them in a cloud build machine, a CI
runner or a shared computer, and never paste a key, a password or a key file into a chat, an
email, an issue or this repository.

## Which keys you need, and when

| Key | Needed for | When to make it |
|---|---|---|
| Watch app release key | Installing a signed watch app on a real watch (requirement D1b.c) | **Now**, before any physical-device testing |
| Family app upload key (Android) | Uploading the phone app to Google Play | When you open a Google Play developer account |
| Apple distribution certificate | Distributing the iOS app (TestFlight or App Store) | When you join the Apple Developer Program (a paid membership) |
| OTA release manifest key | The server signing over-the-air update manifests | When a real server is set up, before any watch receives updates |
| Device-policy instruction key | Deployment administrators sending signed instructions to watches | With the OTA key, when a real server is set up |
| IRA CARE OS platform keys, AVB key, OS OTA key | Building and updating a distributed IRA CARE OS image | **Later**: only when there is a real board and a build host. Development images use AOSP's public test keys, which must never reach a real person's watch |
| Server keys, backup key | Running a real deployment | When a real server is set up (`docs/ops/RUNBOOKS.md`) |

So **today only the watch app release key is worth making**, and only when you are ready to
install the app on a physical watch.

## Before you start

1. Use your own laptop, with full-disk encryption on (FileVault on a Mac, BitLocker on Windows,
   LUKS on Linux) and the operating system up to date.
2. Install the tools:
   - **Java JDK 17**, which provides `keytool` (Adoptium Temurin 17: <https://adoptium.net/>). Check with
     `keytool -help`.
   - **Python 3.11 or later** with the `cryptography` package (`pip install cryptography`), for the
     server keys. Check with `python3 -c "import cryptography; print(cryptography.__version__)"`.
3. Have a password manager ready (for example Bitwarden or 1Password). Every key password goes
   there and nowhere else.
4. Have two USB drives for the encrypted backups, kept in two different places.

## 1. The watch app release key (needed first)

1. Open a terminal in an empty folder **outside** the IRA CARE repository.
2. Run (one line):

   ```bash
   keytool -genkeypair -v -keystore ira-watch-release.jks -alias ira-watch -keyalg RSA -keysize 4096 -validity 10000
   ```

3. When asked, set a long random **keystore password** and a long random **key password**, and
   save both in the password manager straight away. For the name questions, use "IRA CARE" for
   the organisation and "GB" for the country.
4. Record the fingerprint (it is public and safe to write down):

   ```bash
   keytool -list -v -keystore ira-watch-release.jks -alias ira-watch | grep SHA256
   ```

   Add it to the founders' key register (a private note in the password manager): key name,
   fingerprint, date, who made it, where the backups are.
5. Make the two encrypted backups (step 6 below), then move on.

To **use** it when building a signed release on your machine, set these four variables in the
terminal only for that build, then close the terminal:

```bash
export IRA_WATCH_KEYSTORE=/full/path/to/ira-watch-release.jks
export IRA_WATCH_KEYSTORE_PASSWORD='…'
export IRA_WATCH_KEY_ALIAS=ira-watch
export IRA_WATCH_KEY_PASSWORD='…'
cd watch && ./gradlew assembleWearosRelease assembleAospRelease
```

Check the result is signed with your key:

```bash
$ANDROID_HOME/build-tools/<version>/apksigner verify --print-certs app/build/outputs/apk/wearos/release/app-wearos-release.apk
```

The SHA-256 digest printed must match the fingerprint in the key register. (`watch/keystore.properties`
also works instead of the variables; git ignores it, and the repository's tests check that.)

## 2. The family app upload key (when you join Google Play)

Same as step 1 with a separate keystore:

```bash
keytool -genkeypair -v -keystore ira-family-upload.jks -alias ira-family-upload -keyalg RSA -keysize 4096 -validity 10000
```

In Google Play Console, enrol the app in **Play App Signing**. Google then holds the app signing
key and you hold only this upload key, which Google can reset if it is lost.

## 3. The OTA manifest key and the device-policy key (when a real server exists)

These two keys live on the server, never on a laptop for longer than it takes to generate them.
Generate each one in a terminal and put the printed value straight into the server's secret
store or `deploy/.env` on the server, never into git.

OTA manifest key (Ed25519, base64 of a 32-byte seed):

```bash
python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
```

Device-policy instruction key (ECDSA P-256, base64 of the 32-byte private scalar):

```bash
python3 -c "from cryptography.hazmat.primitives.asymmetric import ec;import base64;k=ec.generate_private_key(ec.SECP256R1());print(base64.b64encode(k.private_numbers().private_value.to_bytes(32,'big')).decode())"
```

Set them on the server as `IRA_OTA_MANIFEST_KEY` and `IRA_DEVICE_POLICY_KEY`. The matching
**public** keys are derived by the server code (`care/ota.py`, `care/devicepolicy.py`) and are
built into the watch image so the watch can verify what it receives. Keep an encrypted offline copy
of each private value (step 6).

## 4. IRA CARE OS keys (later, with real hardware)

When there is a real board and an AOSP build host, generate the platform keys on that build host
with AOSP's own tool, one per key name (`releasekey`, `platform`, `shared`, `media`,
`networkstack`, `sdk_sandbox`, `bluetooth`, `nfc`):

```bash
development/tools/make_key releasekey '/C=GB/O=IRA CARE/CN=IRA CARE releasekey'
```

Do the same for the verified-boot key (`openssl genrsa -out avb.pem 4096`, then encrypt it) and the
OS OTA package key. Until then, development images use AOSP's public test keys, which is fine for
engineering but must never be used for a watch that goes to a real person.

## 5. Development and test keys (fine to use now)

- Android's debug key, made automatically by Gradle, signs debug builds. It is not secret and must
  not be used for anything a person outside the team installs.
- Tests generate throwaway OTA and device-policy keys on the fly; nothing to do.
- AOSP test keys sign development OS images only.

## 6. Encrypted backups (do this for every real key)

1. Put the key file (or the printed value saved in a file) into an encrypted container, for
   example with `age` (<https://github.com/FiloSottile/age>):

   ```bash
   age -p -o ira-watch-release.jks.age ira-watch-release.jks
   ```

   Use a different passphrase from the key passwords, and store it in the password manager.
2. Copy the `.age` file to both USB drives, and keep them in two different places.
3. Once a year, restore one copy on a spare machine (`age -d -o test.jks ira-watch-release.jks.age`)
   and check the fingerprint matches, then delete the restored copy.
4. Delete any unencrypted copy that is not needed for signing.

## 7. Who can use a key

Use a two-person rule for release signing: one founder holds the key file, the other the password.
Record every signing (date, version, artefact checksum) in the key register.

## If something goes wrong

- **Lost the watch key, with a backup:** restore from the backup.
- **Lost the watch key, with no backup:** watches already in use cannot receive updates signed by a
  new key, so they must be reinstalled. This is why the two backups matter.
- **A key may have leaked:** follow "If a key is exposed" in [`SIGNING_KEYS.md`](SIGNING_KEYS.md).
  For the OTA and device-policy keys, rotate them the same day.
