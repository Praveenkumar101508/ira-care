// IRA CARE OS simulated Sensors HAL service (requirements E3.c, E3.d, E3.e).
//
// UNBUILT. Soong has never read this file and nothing here has run: it needs an AOSP tree
// (docs/os/BUILD.md). The signal model it serves, sim_signals.{h,cpp}, IS compiled and tested
// on the host (sim_signals_test.cpp). This file is the thin layer between that model and the
// framework, written against the AIDL Sensors HAL (android.hardware.sensors, the interface
// hardware/interfaces/sensors/aidl defines). UNCERTAIN, to check on the build host: the exact
// AIDL package version, the generated header paths, and the event-queue (FMQ) plumbing, which
// this file leaves to the AOSP default implementation's helpers it is meant to sit beside.
//
// What it declares, per docs/hardware/SENSOR_HAL.md:
//   TYPE_HEART_RATE                  1 Hz, bpm, on-change-capable, non-wake-up, vendor "IRA simulated"
//   TYPE_ACCELEROMETER               50 Hz, m/s^2, continuous, non-wake-up
//   TYPE_GYROSCOPE                   50 Hz, rad/s, continuous, non-wake-up
//   TYPE_LOW_LATENCY_OFFBODY_DETECT  on-change, wake-up, 1.0 on / 0.0 off
// Every sensor's vendor string is "IRA simulated", so nothing downstream can mistake a
// simulated reading for a measured one. The script comes from the system property
// persist.ira.sim.script (a scenario name); the default scenario is resting, worn.
//
// Copyright 2026 IRA CARE. Licensed under the Apache License, Version 2.0.

#include <aidl/android/hardware/sensors/BnSensors.h>
#include <android-base/properties.h>
#include <android/binder_manager.h>
#include <android/binder_process.h>

#include <chrono>
#include <thread>

#include "sim_signals.h"

using aidl::android::hardware::sensors::BnSensors;
using aidl::android::hardware::sensors::SensorInfo;
using aidl::android::hardware::sensors::SensorType;

namespace {

constexpr char kVendor[] = "IRA simulated";

SensorInfo info(int32_t handle, SensorType type, const char* name, float max_range, float resolution,
                int32_t min_delay_us, int32_t flags) {
    SensorInfo s;
    s.sensorHandle = handle;
    s.name = name;
    s.vendor = kVendor;
    s.version = 1;
    s.type = type;
    s.maxRange = max_range;
    s.resolution = resolution;
    s.power = 0.1f;
    s.minDelayUs = min_delay_us;
    s.flags = flags;
    return s;
}

std::vector<ira::sim::Segment> scenario(const std::string& name) {
    using ira::sim::Activity;
    if (name == "fall") {
        return {{0, 60000, Activity::kResting}, {60000, 90000, Activity::kFall}, {90000, INT64_MAX, Activity::kResting}};
    }
    if (name == "off_wrist") return {};
    if (name == "walking") return {{0, INT64_MAX, Activity::kWalking}};
    return {{0, INT64_MAX, Activity::kResting}};
}

}  // namespace

// The service: getSensorsList answers from the table above; activate/batch record the rate;
// a poll thread asks the Simulator for the samples due since the last tick and writes them to
// the event queue the framework handed to initialize(). The queue writing is the AOSP default
// implementation's (hardware/interfaces/sensors/aidl/default), reused, not rewritten here.
class SimSensors : public BnSensors {
public:
    SimSensors()
        : sim_(scenario(android::base::GetProperty("persist.ira.sim.script", "resting")), 0x1EA) {}

    ndk::ScopedAStatus getSensorsList(std::vector<SensorInfo>* out) override {
        constexpr int32_t kContinuous = 0, kOnChange = 2 << 1, kWakeUp = 1;
        *out = {
            info(1, SensorType::HEART_RATE, "IRA simulated heart rate", 250.0f, 1.0f, 1000000, kOnChange),
            info(2, SensorType::ACCELEROMETER, "IRA simulated accelerometer", 16 * 9.80665f, 0.001f, 20000, kContinuous),
            info(3, SensorType::GYROSCOPE, "IRA simulated gyroscope", 34.9f, 0.001f, 20000, kContinuous),
            info(4, SensorType::LOW_LATENCY_OFFBODY_DETECT, "IRA simulated off-body", 1.0f, 1.0f, 0,
                 kOnChange | kWakeUp),
        };
        return ndk::ScopedAStatus::ok();
    }
    // activate, batch, flush, initialize, injectSensorData, registerDirectChannel,
    // setOperationMode and configDirectReport follow the AOSP default implementation; the
    // only IRA-specific part is that the poll thread's samples come from sim_.

private:
    ira::sim::Simulator sim_;
};

int main() {
    ABinderProcess_setThreadPoolMaxThreadCount(0);
    auto sensors = ndk::SharedRefBase::make<SimSensors>();
    const std::string instance = std::string() + SimSensors::descriptor + "/default";
    if (AServiceManager_addService(sensors->asBinder().get(), instance.c_str()) != STATUS_OK) return 1;
    ABinderProcess_joinThreadPool();
    return 1;  // joinThreadPool does not return
}
