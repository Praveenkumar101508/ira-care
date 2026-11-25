// IRA CARE OS simulated sensors: the signal model (requirements E3.c, E3.d, E3.e).
//
// Pure C++17, no Android dependency, so it is compiled and tested on any host
// (sim_signals_test.cpp, run by supracloud-jarvis/ira/tests/readiness/test_os_sensor_sim.py).
// The Sensors HAL service in SimSensorsHal.cpp wraps it; that service is UNBUILT (it needs an
// AOSP tree) and has never run.
//
// Every value follows docs/hardware/SENSOR_HAL.md: heart rate in beats per minute with an
// honest status (NO_CONTACT off the wrist), acceleration in m/s^2 and angular rate in rad/s on
// the device frame's three axes, off-body 1.0 on the body and 0.0 off. Everything a simulated
// sensor reports is marked simulated by the HAL (vendor string "IRA simulated").
//
// Deterministic: the same seed and script give the same samples, so a fall demonstration
// or a regression test can be replayed exactly.
//
// Copyright 2026 IRA CARE. Licensed under the Apache License, Version 2.0.
#pragma once

#include <array>
#include <cstdint>
#include <vector>

namespace ira::sim {

constexpr double kGravity = 9.80665;  // SensorManager.GRAVITY_EARTH

enum class Status : int8_t {  // the values of SensorManager.SENSOR_STATUS_*
    kNoContact = -1,
    kUnreliable = 0,
    kAccuracyLow = 1,
    kAccuracyMedium = 2,
    kAccuracyHigh = 3,
};

// What the wearer is doing between two times (milliseconds since the script starts).
enum class Activity { kOffWrist, kResting, kWalking, kFall };

struct Segment {
    int64_t start_ms;
    int64_t end_ms;
    Activity activity;
};

struct HeartSample {
    int64_t t_ms;
    float bpm;
    Status status;
};

struct MotionSample {  // accelerometer (m/s^2) or gyroscope (rad/s)
    int64_t t_ms;
    std::array<float, 3> v;
};

struct OffBodyEvent {
    int64_t t_ms;
    float on_body;  // 1.0 on the body, 0.0 off
};

class Simulator {
public:
    Simulator(std::vector<Segment> script, uint64_t seed, float resting_bpm = 68.0f);

    Activity activity_at(int64_t t_ms) const;

    // Heart rate at 1 Hz (the contract's rate while worn), from t0 to t1.
    std::vector<HeartSample> heart_rate(int64_t t0_ms, int64_t t1_ms);
    // Accelerometer and gyroscope at `hz` (50 Hz for the fall detectors).
    std::vector<MotionSample> accelerometer(int64_t t0_ms, int64_t t1_ms, int hz = 50);
    std::vector<MotionSample> gyroscope(int64_t t0_ms, int64_t t1_ms, int hz = 50);
    // Off-body changes only (the sensor is on-change), with the first state at t0.
    std::vector<OffBodyEvent> off_body(int64_t t0_ms, int64_t t1_ms) const;

private:
    double noise();  // deterministic, roughly N(0, 1)
    std::vector<Segment> script_;
    uint64_t state_;
    float resting_bpm_;
};

}  // namespace ira::sim
