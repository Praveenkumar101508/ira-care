# Stand-in device test: Samsung Galaxy Watch 4

For Praveen. One run of the watch care journey on your own Galaxy Watch 4 (Wear OS 6, One UI 8
Watch), with your Android phone and no computer. Results are recorded as **stand-in device:
Samsung Galaxy Watch 4 (SM-R8xx), Wear OS 6**, never as IRA watch validation (rules at the end).
Fictional data only; texts, calls and SOS messages are logged on the test server, never sent.
**No real falls:** the fall step uses the debug build's synthetic fall.

## 1. Start the test server

GitHub, **Actions**, **watch device test server (manual)**, **Run workflow**, choose the hours
(default 3), **Run workflow**. After about 15 minutes the run's **Summary** shows the address
(`https://….trycloudflare.com`), and the artifact **ira-watch-test-apk** is on the same page.
It stops by itself at the time shown. Every run has a new address. The workflow is
`.github/workflows/watch-device-test.yml`; why this tunnel: `docs/evidence/device/TUNNEL_CHOICE.md`.

## 2. Install the APK from your phone (no computer)

1. On the phone, open the run page in the browser (signed in to GitHub), download
   **ira-watch-test-apk** and extract the zip in the Files app: you get `app-wearos-debug.apk`.
2. Watch: **Settings, About watch, Software information**, tap **Software version** five times
   (developer options on). Then **Settings, Developer options**: **ADB debugging** on,
   **Wireless debugging** on. Watch and phone on the same Wi-Fi.
3. Watch: **Wireless debugging, Pair new device**: note the pairing code, IP address and port.
4. Phone, **Bugjaeger** (or **Wear Installer 2**): connect over Wi-Fi with that IP, port and
   pairing code, then connect to the address shown under **Wireless debugging** (its own port).
5. Install `app-wearos-debug.apk` (Bugjaeger: the install-APK button; Wear Installer 2: choose
   the file, **Install**). The watch app is **IRA CARE** (package `care.ira.watch.debug`).

Typing long text on the watch is slow. With the field selected on the watch, Bugjaeger's
**Shell** can type it for you: `input text 'https://….trycloudflare.com'`.

## 3. Pair the watch to the test server

1. Phone browser: open the address followed by **/login**, sign in as `praveen@example.com` with the password you
   saved as the repository secret `IRA_WATCH_TEST_DASHBOARD_PASSWORD`.
2. **Ada**, tab **Health and wellbeing**, **Create a pairing code**. It works once, for 15 minutes.
   Never post it anywhere public.
3. Watch, IRA CARE: **Test server…**, type the address (https only), **Save**.
4. Type the pairing code (or `input text '<code>'` from Bugjaeger's Shell), **Pair**.

## 4. Checklist

For every step send me: **step number, pass or fail, one line of notes**, and the screenshot
named. Watch screenshot: press **Home and Back together** (it lands in the watch's Gallery; send
it to the phone), or Bugjaeger's screenshot. Dashboard screenshot: the phone's own.

| # | Step | What you should see | Send | Requirement |
|---|---|---|---|---|
| 1 | Install and launch | IRA CARE opens on **Pair this watch** with **Test server…** under it | watch | D1.j |
| 2 | Test server and pairing | "Paired.", then the permission screen | watch | D1.j |
| 3 | Permissions | Allow body sensors, physical activity, microphone, notifications, location; on the settings page it opens, body sensors **All the time**. Home screen with **I'm OK** and **Help** | watch (home) | D1.j |
| 4 | Heart rate shown | After about 10 minutes worn: dashboard, Ada, **Health and wellbeing**, the **heart rate** card is marked **fresh** with a value from your wrist (the older seeded readings do not count) | dashboard | D1.j |
| 5 | Check-in spoken and answered by voice | Wear it and wait (a check-in comes by itself every 15 to 20 minutes outside quiet hours). The watch vibrates and speaks the question; answer aloud ("Yes, I'm fine"). Dashboard, Ada, **Timeline** shows your answer | watch + dashboard | C-17.b, D1.j |
| 6 | Interrupted answer | At the next check-in, start answering, then press the side **Back** button (or cover the screen) mid-answer. The app comes back to the question or asks again; nothing is recorded as an answer you did not give | watch + dashboard | C-17.b |
| 7 | Help pressed and acknowledged | Press **Help**: the watch shows and says "Help request received." Dashboard, **Alerts**: "Asked for help"; press **I'm on my way**: within a minute the watch shows "… is on the way." with your name. Then **Record the outcome** | watch (both states) + dashboard | D1.j |
| 8 | Synthetic fall, countdown and voice | Bugjaeger Shell: `am broadcast -n care.ira.watch.debug/care.ira.watch.debug.SyntheticFallReceiver -a care.ira.watch.debug.SYNTHETIC_FALL --ef peak_g 3.2 --ei still_seconds 20`. The watch vibrates, asks aloud whether you are OK and counts down 30 s. Answer **I'm OK**: the countdown stops, nothing escalates. Run it again and let it reach 0: the dashboard shows a fall alert | watch (countdown) + dashboard | C-5.b |
| 9 | Network off, then on | Watch: turn Wi-Fi and Bluetooth off (or Airplane mode). Press **Help**: the watch shows "Waiting for connection. Help request saved." After 1 minute turn them back on: the dashboard shows **one** new Help alert, not two | watch (waiting) + dashboard | D1.j |
| 10 | Restart recovery | Restart the watch. Without opening anything, IRA CARE is running and still paired; Help still works | watch | D1.j |
| 11 | Unpairing | Home, **Unpair watch…**: a warning with **Keep paired** first. **Unpair**: the watch returns to pairing; dashboard shows no paired watch | watch (warning) + dashboard | C-21.d |
| 12 | TalkBack | Pair again (steps 3.2 to 3.4). **Settings, Accessibility, TalkBack** on. Swipe through home: every button is read with its label ("Help. Tells your family and carers you need them now.", "I'm OK. …", "Unpair this watch. …"); double-tap **I'm OK** works. TalkBack off afterwards | watch | C-23.e (watch part) |

If step 7's "on the way" never arrives, say so: the free tunnel may hold back the server's live
stream (`docs/evidence/device/TUNNEL_CHOICE.md`); it is still a fail for that step.

## 5. How results are recorded

In `docs/progress/requirements.json`, under its existing rules, with the label **stand-in device:
Samsung Galaxy Watch 4 (SM-R8xx), Wear OS 6**, as `device` evidence pointing to a results file
in `docs/evidence/device/`. A requirement moves only when every step mapped to it passed. Nothing
is recorded as IRA watch validation. F3.e, F4.g and F7.k ("on the IRA watch") and F3.d (raw PPG)
stay blocked whatever this run shows. No denominator changes.
