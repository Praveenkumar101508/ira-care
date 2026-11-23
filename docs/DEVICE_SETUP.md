# IRA CARE: setting up the watch

This is the carer's guide to a new watch. Everything here has been exercised only on the
build machine and in unit tests: **no watch has run this app yet** (see
`progress/STATUS.md`, "Needs the owner"). Treat the steps as the plan to verify on the
first real device, and correct this page when they differ.

## 1. Pair the watch

1. In the dashboard (People, the person, Watch) or the family app (the person, "Pair a new
   watch"), create a pairing code. It is valid for 15 minutes and works once.
2. Install the IRA CARE watch app, open it, and type the code. The watch keeps a device
   token; the code is never stored.
3. Allow the permissions the watch asks for: body sensors (heart rate), physical activity
   (steps), microphone (so the person can answer by voice), notifications, and location.
   Then open the system settings page the app sends you to and allow **body sensors in the
   background**; without it the watch cannot read the heart rate while the screen is off.
4. Keep the app's ongoing notification: it is the foreground service that collects
   readings and asks the check-in questions.

Location is read **once**, at the moment help is asked for or a suspected fall is
confirmed, and sent with that request. It is never read in the background and never
streamed. If the watch has no position at that moment the alert says "location
unavailable"; it never guesses.

## 2. Fall detection in IRA CARE

The IRA CARE app watches the accelerometer for a hard impact followed by no movement for
a while (by default an impact of 2.5 g and then 20 seconds of stillness). When that
happens the watch vibrates, says "It looks like you may have fallen. Are you OK?", and
shows two big buttons for a countdown (30 seconds by default). "I'm OK" cancels. "Help",
or the countdown ending, opens an SOS with the watch's location, exactly like the Help
button.

- It is **on by default** and a carer can turn it off for a person on the dashboard's
  Settings tab, where the countdown and the thresholds are also set.
- The thresholds are **settings, not medical claims**. The detector has been tested on
  synthetic signals only; its accuracy on real people is unmeasured until real devices
  are used (`progress/STATUS.md` has the synthetic counts and that warning).
- When the watch knows it is off the wrist, nothing is detected: a watch dropped on a
  table is the classic false alarm.

## 3. Turn on the watch maker's own fall detection too

IRA CARE's detector is one layer. Every current smartwatch platform ships its own
hard-fall detection, built by the maker with far more real-world data than we have. Turn
it on as a second, independent layer. The menu names below change between models and
software versions; look for "fall" in the watch's settings search if they differ.

- **Samsung Galaxy Watch (Wear OS):** on the watch, Settings, then Safety and emergency,
  then Hard fall detection. Choose whether it is always on or only during workouts, and set
  the emergency contacts there (the maker's feature calls or messages contacts of its own;
  IRA CARE never dials emergency services itself).
- **Google Pixel Watch:** open the Personal Safety app on the watch, then Fall detection,
  and turn it on. It is off by default and asks for emergency contacts of its own.
- **Apple Watch (for the future Apple plan, `APPLE_WATCH_PLAN.md`):** on the iPhone, Watch
  app, Emergency SOS, Fall Detection. For people aged 55 and over it is on by default once
  the age is in the Health profile.

Having both on means two different detectors and two different ways to reach somebody.
That is the point: neither one is a guarantee.

## 4. Check it works

On the dashboard, the person's Watch line shows the battery, whether the watch is on the
wrist and when it last synced. Within a few minutes of pairing you should see readings on
the Readings tab. Ask IRA to check in now (family app or dashboard) and answer on the
watch; the check-in appears on the timeline. Then, with the family told first, press Help
on the watch and confirm the alert reaches the family app with the address and location.

## What the watch says during an SOS

The wrist shows only what is true. After Help, or when a fall countdown ends, the request
is saved on the watch first and the screen says **Sending…**; if there is no connection it
says **Waiting for connection. Help request saved.** and keeps retrying (the request
survives a restart and a reboot). **Help request received.** appears only once the server
has confirmed it. **[Name] is on the way.** appears when someone taps "I'm on my way" in the
family app or on the dashboard, and **[Name] has arrived.** when they tap "I've arrived".
The server pushes these changes to the watch over a held connection; the watch reconnects
when it drops. Ordinary readings go up every 60 seconds (the per-person setting
`reading_upload_interval_s`); a heart-rate sample outside the range the carer set goes up
at once.
