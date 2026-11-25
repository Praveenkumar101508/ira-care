// Host test of the simulated sensors' signal model against docs/hardware/SENSOR_HAL.md.
// Built and run by supracloud-jarvis/ira/tests/readiness/test_os_sensor_sim.py with the host's
// C++ compiler; prints one line per check and exits non-zero on the first failure.
// Copyright 2026 IRA CARE. Licensed under the Apache License, Version 2.0.
#include <cmath>
#include <cstdio>
#include <cstdlib>

#include "sim_signals.h"

using namespace ira::sim;

static int failures = 0;
#define CHECK(cond, what)                                  \
    do {                                                   \
        if (cond) {                                        \
            std::printf("ok   %s\n", what);                \
        } else {                                           \
            std::printf("FAIL %s\n", what);                \
            ++failures;                                    \
        }                                                  \
    } while (0)

static double mag(const MotionSample& s) {
    return std::sqrt(s.v[0] * s.v[0] + s.v[1] * s.v[1] + s.v[2] * s.v[2]);
}

int main() {
    // 0-60 s off the wrist, 60-180 s resting, 180-300 s walking, a fall at 300 s, lying until 330 s.
    const std::vector<Segment> script = {
        {60000, 180000, Activity::kResting},
        {180000, 300000, Activity::kWalking},
        {300000, 330000, Activity::kFall},
    };
    Simulator sim(script, 42);

    // E3.c heart rate: 1 Hz, plausible while worn, NO_CONTACT (and no value) off the wrist.
    auto hr = sim.heart_rate(0, 330000);
    CHECK(hr.size() == 330, "heart rate is one sample a second");
    bool off_ok = true, worn_ok = true, walk_higher = true;
    double rest_sum = 0, walk_sum = 0; int rest_n = 0, walk_n = 0;
    for (const auto& s : hr) {
        const Activity a = sim.activity_at(s.t_ms);
        if (a == Activity::kOffWrist) {
            off_ok &= s.status == Status::kNoContact && s.bpm == 0.0f;
        } else {
            worn_ok &= s.status > Status::kUnreliable && s.bpm >= 25.0f && s.bpm <= 250.0f;
            if (a == Activity::kResting) { rest_sum += s.bpm; ++rest_n; }
            if (a == Activity::kWalking) { walk_sum += s.bpm; ++walk_n; }
        }
    }
    walk_higher = walk_sum / walk_n > rest_sum / rest_n + 10.0;
    CHECK(off_ok, "off the wrist the heart rate is NO_CONTACT with no value");
    CHECK(worn_ok, "worn, every heart rate is between 25 and 250 bpm with a usable status");
    CHECK(walk_higher, "walking raises the heart rate over resting");

    // E3.d accelerometer and gyroscope: 50 Hz, m/s^2, gravity at rest, a fall the detector sees.
    auto acc = sim.accelerometer(60000, 70000);
    CHECK(acc.size() == 500, "accelerometer is 50 samples a second");
    bool gravity = true;
    for (const auto& s : acc) gravity &= std::fabs(mag(s) - kGravity) < 0.3;
    CHECK(gravity, "at rest the acceleration magnitude is gravity (m/s^2, not g)");
    auto fall = sim.accelerometer(299000, 330000);
    double peak = 0; int64_t peak_t = 0;
    for (const auto& s : fall) if (mag(s) > peak) { peak = mag(s); peak_t = s.t_ms; }
    CHECK(peak > 2.5 * kGravity, "the simulated fall's impact exceeds the rule detector's 2.5 g");
    bool still = true;
    for (const auto& s : fall) if (s.t_ms > peak_t + 1000 && s.t_ms < peak_t + 21000) still &= std::fabs(mag(s) - kGravity) < 0.2;
    CHECK(still, "after the impact the wrist lies still for the detector's 20 s window");
    auto gyro = sim.gyroscope(180000, 182000);
    double gmax = 0;
    for (const auto& s : gyro) gmax = std::fmax(gmax, std::fabs(s.v[0]));
    CHECK(gyro.size() == 100 && gmax > 0.5 && gmax < 10.0, "gyroscope is 50 Hz in rad/s, moving while walking");

    // E3.e off-body: on-change, the first state at the start, each change within 100 ms.
    auto ob = sim.off_body(0, 400000);
    CHECK(ob.size() == 3, "off-body reports only changes (off, on, off)");
    CHECK(ob[0].t_ms == 0 && ob[0].on_body == 0.0f, "starts off the wrist");
    CHECK(ob[1].t_ms == 60000 && ob[1].on_body == 1.0f, "put on at 60 s, reported at once");
    CHECK(ob[2].t_ms == 330000 && ob[2].on_body == 0.0f, "taken off at 330 s, reported at once");

    // Deterministic: the same seed replays the same samples; another seed does not.
    Simulator a(script, 7), b(script, 7), c(script, 8);
    auto ha = a.heart_rate(60000, 70000), hb = b.heart_rate(60000, 70000), hc = c.heart_rate(60000, 70000);
    bool same = true, differs = false;
    for (size_t i = 0; i < ha.size(); ++i) { same &= ha[i].bpm == hb[i].bpm; differs |= ha[i].bpm != hc[i].bpm; }
    CHECK(same && differs, "the same seed replays exactly; another seed differs");

    std::printf("%s: %d failure(s)\n", failures ? "FAILED" : "PASSED", failures);
    return failures ? 1 : 0;
}
