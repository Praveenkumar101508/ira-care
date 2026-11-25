# IRA CARE OS simulated sensors (E3.c, E3.d, E3.e)

**State: the signal model is built and tested on the host; the HAL service is written and
unbuilt.** Nothing here has run on Cuttlefish or any device.

| File | What it is | State |
| --- | --- | --- |
| `sim_signals.h`, `sim_signals.cpp` | The deterministic signal model: heart rate (1 Hz, bpm, `NO_CONTACT` off the wrist), accelerometer and gyroscope (50 Hz, m/s² and rad/s, a scripted fall with free fall, an impact over 2.5 g and stillness), off-body (on-change, 1.0 / 0.0) | compiled and tested on the host by `supracloud-jarvis/ira/tests/readiness/test_os_sensor_sim.py` |
| `sim_signals_test.cpp` | The host test, checked against `docs/hardware/SENSOR_HAL.md` | run by the test above |
| `SimSensorsHal.cpp`, `Android.bp` | The AIDL Sensors HAL service around the model; every sensor's vendor string is "IRA simulated" | unbuilt; the AIDL package version, header paths and the event-queue plumbing must be checked on a build host |

For a simulated image only, on the build host: add `android.hardware.sensors-service.ira-sim`
to `PRODUCT_PACKAGES`, with its VINTF manifest fragment and init `.rc` (the AOSP default sensors
service's are the pattern), and set `persist.ira.sim.script` to `resting`, `walking`, `fall` or
`off_wrist`. Never in an image a person wears: a real board's vendor HAL replaces it.
