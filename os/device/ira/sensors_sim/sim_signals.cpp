// See sim_signals.h. Copyright 2026 IRA CARE. Licensed under the Apache License, Version 2.0.
#include "sim_signals.h"

#include <cmath>

namespace ira::sim {

Simulator::Simulator(std::vector<Segment> script, uint64_t seed, float resting_bpm)
    : script_(std::move(script)), state_(seed ? seed : 0x9E3779B97F4A7C15ull), resting_bpm_(resting_bpm) {}

Activity Simulator::activity_at(int64_t t_ms) const {
    for (const auto& s : script_) {
        if (t_ms >= s.start_ms && t_ms < s.end_ms) return s.activity;
    }
    return Activity::kOffWrist;  // outside the script the watch is on the table
}

double Simulator::noise() {
    // xorshift64*, then a sum of uniforms for a bell shape. Deterministic per seed.
    double sum = 0.0;
    for (int i = 0; i < 4; ++i) {
        state_ ^= state_ >> 12;
        state_ ^= state_ << 25;
        state_ ^= state_ >> 27;
        const uint64_t r = state_ * 0x2545F4914F6CDD1Dull;
        sum += static_cast<double>(r >> 11) * (1.0 / 9007199254740992.0);
    }
    return (sum - 2.0) * std::sqrt(3.0);
}

std::vector<HeartSample> Simulator::heart_rate(int64_t t0_ms, int64_t t1_ms) {
    std::vector<HeartSample> out;
    for (int64_t t = t0_ms; t < t1_ms; t += 1000) {
        const Activity a = activity_at(t);
        if (a == Activity::kOffWrist) {
            // No optical contact: the HAL says so, and the app drops the sample (never recorded).
            out.push_back({t, 0.0f, Status::kNoContact});
            continue;
        }
        double target = resting_bpm_;
        if (a == Activity::kWalking) target += 22.0;
        if (a == Activity::kFall) target += 35.0;
        const double slow = 3.0 * std::sin(2.0 * M_PI * static_cast<double>(t) / 300000.0);
        double bpm = target + slow + 1.2 * noise();
        if (bpm < 25.0) bpm = 25.0;
        if (bpm > 250.0) bpm = 250.0;
        out.push_back({t, static_cast<float>(bpm), a == Activity::kFall ? Status::kAccuracyLow : Status::kAccuracyHigh});
    }
    return out;
}

std::vector<MotionSample> Simulator::accelerometer(int64_t t0_ms, int64_t t1_ms, int hz) {
    std::vector<MotionSample> out;
    const double step = 1000.0 / hz;
    for (double tt = static_cast<double>(t0_ms); tt < static_cast<double>(t1_ms); tt += step) {
        const auto t = static_cast<int64_t>(tt);
        const Activity a = activity_at(t);
        // Resting wrist: gravity mostly on z, small tremor.
        double x = 0.4, y = 0.6, z = kGravity;
        double jitter = 0.03;
        if (a == Activity::kOffWrist) {
            jitter = 0.005;  // flat on a table
        } else if (a == Activity::kWalking) {
            const double s = std::sin(2.0 * M_PI * 1.8 * tt / 1000.0);  // about 1.8 steps a second
            x += 2.5 * s;
            z += 1.5 * std::fabs(s);
            jitter = 0.3;
        } else if (a == Activity::kFall) {
            // Find where this fall segment started: free fall, an impact, then stillness on the floor.
            int64_t start = t;
            for (const auto& s : script_) {
                if (t >= s.start_ms && t < s.end_ms) start = s.start_ms;
            }
            const int64_t dt = t - start;
            if (dt < 400) {          // free fall: near-weightless
                x = 0.2; y = 0.2; z = 1.5;
            } else if (dt < 520) {   // impact: well over the 2.5 g the rule detector looks for
                x = 18.0; y = -14.0; z = 30.0;
            } else {                 // lying still on the floor, wrist on its side
                x = kGravity; y = 0.3; z = 0.5;
                jitter = 0.01;
            }
        }
        out.push_back({t, {static_cast<float>(x + jitter * noise()), static_cast<float>(y + jitter * noise()),
                           static_cast<float>(z + jitter * noise())}});
    }
    return out;
}

std::vector<MotionSample> Simulator::gyroscope(int64_t t0_ms, int64_t t1_ms, int hz) {
    std::vector<MotionSample> out;
    const double step = 1000.0 / hz;
    for (double tt = static_cast<double>(t0_ms); tt < static_cast<double>(t1_ms); tt += step) {
        const auto t = static_cast<int64_t>(tt);
        const Activity a = activity_at(t);
        double amp = a == Activity::kWalking ? 1.2 : (a == Activity::kFall ? 4.0 : 0.02);
        if (a == Activity::kOffWrist) amp = 0.002;
        const double s = std::sin(2.0 * M_PI * 1.8 * tt / 1000.0);
        out.push_back({t, {static_cast<float>(amp * s + 0.01 * noise()), static_cast<float>(0.5 * amp * s + 0.01 * noise()),
                           static_cast<float>(0.01 * noise())}});
    }
    return out;
}

std::vector<OffBodyEvent> Simulator::off_body(int64_t t0_ms, int64_t t1_ms) const {
    std::vector<OffBodyEvent> out;
    float last = -1.0f;
    for (int64_t t = t0_ms; t < t1_ms; t += 100) {  // a change is reported within 100 ms (contract: 1 s)
        const float on = activity_at(t) == Activity::kOffWrist ? 0.0f : 1.0f;
        if (on != last) {
            out.push_back({t, on});
            last = on;
        }
    }
    return out;
}

}  // namespace ira::sim
