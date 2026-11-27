# IRA CARE OS simulated sensors (E3.c, E3.d, E3.e)

**State: the signal model is built and tested on the host; the HAL service is written and
unbuilt.** Nothing here has run on Cuttlefish or any device.

| File | What it is | State |
| --- | --- | --- |
| `sim_signals.h`, `sim_signals.cpp` | The deterministic signal model: heart rate (1 Hz, bpm, `NO_CONTACT` off the wrist), accelerometer and gyroscope (50 Hz, m/s² and rad/s, a scripted fall with free fall, an impact over 2.5 g and stillness), off-body (on-change, 1.0 / 0.0) | compiled and tested on the host by `supracloud-jarvis/ira/tests/readiness/test_os_sensor_sim.py` |
| `sim_signals_test.cpp` | The host test, checked against `docs/hardware/SENSOR_HAL.md` | run by the test above |
| `SimSensorsHal.cpp`, `Android.bp` | The AIDL Sensors HAL service around the model; every sensor's vendor string is "IRA simulated"; reporting-mode flags from `SensorInfo.SENSOR_FLAG_BITS_*` (on-change = 2, wake-up = 1) | unbuilt; the AIDL package version (V3, as the Android 15 default service), header paths and the event-queue plumbing must be checked on a build host |
| `android.hardware.sensors-service.ira-sim.rc`, `android.hardware.sensors-ira-sim.xml` | init script and VINTF fragment (interface version 3, `ISensors/default`), the shape of the AOSP default service's | unbuilt; host test checks the fragment's version equals the `-V3-ndk` library in `Android.bp` |

For a simulated image only, on the build host: add `android.hardware.sensors-service.ira-sim`
to `PRODUCT_PACKAGES` (its VINTF fragment and init `.rc` come with the module) and set
`persist.ira.sim.script` to `resting`, `walking`, `fall` or `off_wrist`. Never in an image a
person wears: a real board's vendor HAL replaces it.

What the host checks prove and do not prove: `docs/os/HOST_CHECKS.md`.
